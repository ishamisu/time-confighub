# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""
Orchestrator — single entry point for all TCH service commands and pipeline workflows.

Non-obvious assumptions:
- ``execute()`` resets internal log/error state on every call; not safe to call
  concurrently on the same instance.
- ``PIPELINE`` commands require ``ServiceRequest.pipeline_config`` to be set;
  a failure result is returned immediately if it is ``None``.
- For B2B / MULTI_DUT, all targets must complete stage N before any target
  begins stage N+1; a single stage failure aborts the remaining pipeline.
- Step-level ``threading.Barrier`` is injected into ``StageContext.barrier`` only
  when more than one target participates in a step; single-target steps receive
  ``barrier=None``.

Logging design:
  Each concurrent step thread is tagged with a ``[role/target_id]`` prefix via a
  thread-local filter so log records from any service logger are identifiable
  per-target — installed once at import time via ``logging_context``.

  Log flow::

    orchestrator / time_config_hub logger namespaces
              │
              ▼
        _RoleFilter (logging_context.py)
        └── prepends [role/target_id] from threading.local()
              │
              ▼
        handler (file / stream / syslog)

  Per-thread context is set by calling ``set_role_log_context(target)`` at the
  entry point of every step thread before any logging occurs.

Stage-results workflow:
  Each stage accumulates per-target output across all its steps into
  ``_stage_results[target_id][stage_name]`` — written unconditionally so partial
  output is available even when a step fails mid-stage (e.g. for live run-stage
  data feeds).

  Data flow::

    _execute_stage_steps(stage_name, steps, targets)
        │
        ├─ for each step → spawn thread(s) per target
        │       │
        │       └─ _run_step(target, action, ctx)
        │               │  collects List[str] output from StageHandler
        │               └─ with lock: stage_output[target.id].extend(output)
        │                             stage_failed = True  (on error)
        │
        └─ after all steps (success or failure)
                │
                ▼
          for target in targets:
            StageResult(target_id, success=not stage_failed,
                        output=stage_output[target.id])
                │
                ▼
          _stage_results[target.id][stage_name] = StageResult
                │
                ▼
          callers query: _stage_results[target_id][stage_name]
          (e.g. live updates during "run" stage)

Side effects:
- Importing this module triggers ``_install_role_filter()`` via ``logging_context``,
  attaching a thread-local log prefix filter to the ``orchestrator`` and
  ``time_config_hub`` logger namespaces.

Related modules:
- logging_context.py — timestamp(), now_ms(), set_role_log_context(): shared thread utilities
- target_manager.py  — resolve_targets(), register_targets(): topology and SSH setup
- ipc.py             — Unix socket server and client helpers
"""

import logging
import threading
from collections import defaultdict
from typing import Optional, Sequence

from time_config_hub.infra.linux.service_manager import ServiceManager

from .models import (
    DeploymentTopologyType,
    PipelineConfig,
    ServiceResult,
    ServiceCommand,
    ServiceRequest,
    StageContext,
    StageHandler,
    StageResult,
    Target,
)

from .single_target import get_steps as get_single_target_steps
from .multi_target import get_steps as get_multi_target_steps
from .logging_context import timestamp, set_role_log_context
from .target_manager import connect_remote_targets, assign_target_roles
from .service_factory import ServiceFactory
from .service_handler import handle_service_request

logger = logging.getLogger("orchestrator")
logger.setLevel(logging.DEBUG)  # Orchestrator logs are always DEBUG level for maximum visibility

# ======================================================================
# Orchestrator — coordinates topology, workers, and result aggregation
# ======================================================================

class Orchestrator:
    """Coordinates stage execution and progress reporting for TCH workflows.

    Owns and self-initialises its :class:`TimeHubService` backend from
    ``tch_app.conf``; YANG module paths and folder drop-in locations are
    fixed by that file and must not be overridden at construction time.

    Usage::

        orch = Orchestrator()
        result = orch.execute(ServiceRequest(...))
    """

    def __init__(
        self,
        pipeline_config: Optional[PipelineConfig] = None,   # PipelineConfig (per-run workflow configuration, passed via ServiceRequest.pipeline_config)
    ):
        """Initialise the orchestrator and its internal :class:`TimeHubService`.

        Configuration is always loaded from ``tch_app.conf`` via
        :meth:`TimeHubService.from_default_config`; the YANG modules and
        folder drop-in paths defined there are not overridable here.
        *pipeline_config* is only needed for the IPC-daemon pipeline path;
        most callers pass it later via :meth:`execute`.

        :param pipeline_config: Pipeline configuration for workflow runs; may also be set
            via ``execute(ServiceRequest(pipeline_config=...))``.
        """
        from .time_hub_service import TimeHubService  # local import avoids circular

        self.pipeline_config = pipeline_config
        self._logs: list[str] = []
        self._errors: list[str] = []
        self._stage_results: dict[str, dict[str, StageResult]] = {}

        self._hub_service = TimeHubService.from_default_config()
        self._service_factory = ServiceFactory(self._hub_service.tch_config)

    # ------------------------------------------------------------------
    # Internal Service Access
    # ------------------------------------------------------------------


    # -- internal helpers ----------------------------------------------

    def _log(self, message: str):
        """Log a timestamped message at INFO level and save it to the internal log."""

        stamped_msg = f"[{timestamp()}] {message}"
        logger.info("%s", stamped_msg)
        self._logs.append(stamped_msg)


    # -- Workflow Steps Execution -----------------------------------

    def _execute_stage_steps(
        self,
        stage_name: str,
        steps: Sequence[tuple[str, set[str | None], StageHandler]],
        targets: list[Target],
    ) -> bool:
        """Execute an ordered sequence of sub-steps for one stage across all targets.

        Within each step, matching-role targets run in parallel on daemon threads.
        A :class:`threading.Barrier` is injected into ``ctx.barrier`` when more
        than one target participates, enabling mid-step rendezvous (e.g. timesync
        validation). On failure, the barrier is aborted to unblock waiting peers
        before the step is abandoned.

        Multi-threading model per step::

            main thread
                │
                ├─ create threading.Lock()       ← protects: stage_output, errors
                ├─ create threading.Barrier(n)   ← only when n_targets > 1
                │
                ├─ for each target in step_targets:
                │       └─ Thread(target=_run_step, name="role/target_id", daemon=True).start()
                │
                │   ┌──────────────────────────────────────────────────────────┐
                │   │  Thread A  (e.g. reference/dut-01)                       │
                │   │    set_role_log_context(target)                           │
                │   │    action(StageContext) → output                          │
                │   │    with lock: stage_output[target.id].extend(output)      │
                │   │    [optional] ctx.barrier.wait()  ◄──── rendezvous point  │
                │   │      └─ raises BrokenBarrierError if peer failed          │
                │   └──────────────────────────────────────────────────────────┘
                │   ┌──────────────────────────────────────────────────────────┐
                │   │  Thread B  (e.g. mirror/dut-02)                          │
                │   │    set_role_log_context(target)                           │
                │   │    action(StageContext) → output   (concurrent with A)    │
                │   │    with lock: stage_output[target.id].extend(output)      │
                │   │    [optional] ctx.barrier.wait()  ◄──── rendezvous point  │
                │   │      └─ raises BrokenBarrierError if peer failed          │
                │   └──────────────────────────────────────────────────────────┘
                │
                ├─ th.join(timeout=config.timeout)  for each thread
                │       ├─ th.is_alive() after join → timeout error recorded
                │       └─ exception in thread → barrier.abort() + error recorded
                │
                ├─ errors?
                │       ├─ Yes → stage_failed = True, break step loop
                │       └─ No  → continue to next step
                │
                └─ [after all steps] write StageResult per target (success or partial)

        Failure isolation:
        - One thread's exception calls ``barrier.abort()``, which unblocks any
          peer blocked on ``barrier.wait()`` with a ``BrokenBarrierError`` rather
          than hanging until timeout.
        - Both exception paths (``Exception`` and ``BrokenBarrierError``) append
          to the shared ``errors`` list under ``lock`` to avoid data races.

        :param str stage_name: Stage label used in log output.
        :param steps: Ordered list of ``(step_name, roles, action)`` tuples.
        :param targets: All active targets for this topology.
        :return: ``True`` if every step completed without error.
        :rtype: bool
        """
        if self.pipeline_config is None:
            raise RuntimeError("_execute_workflow requires pipeline_config")

        _pipeline_config = self.pipeline_config  # captured so closures see a non-Optional reference
        _LOG_BANNER = "─" * 60

        # Accumulate per-target output across all steps in this stage.
        # Keyed by target_id; used to build StageResult at the end.
        stage_output: dict[str, list[str]] = defaultdict(list)  # e.g. ("dut-1" → ["output line 1", "output line 2", ...])
        stage_failed = False

        for step_name, roles, action in steps:
            step_targets = [t for t in targets if t.role in roles]
            if not step_targets:
                self._log(f"  Step '{step_name}' — no matching targets, skipping")
                continue

            role_labels = "/".join(sorted(r for r in roles if r is not None) or ["local"])
            self._log(f"{_LOG_BANNER}")
            self._log(f"STEP  {stage_name} › {step_name}  [{role_labels}]  targets={[t.id for t in step_targets]}")
            self._log(f"{_LOG_BANNER}")

            # Run this step on matching targets in parallel.
            # A Barrier is created when more than one target participates so that
            # handlers (e.g. validate_timesync) can rendezvous mid-step via ctx.barrier.
            errors: list[str] = []
            lock = threading.Lock()
            n_targets = len(step_targets)
            barrier = threading.Barrier(n_targets) if n_targets > 1 else None
            if barrier is not None:
                logger.info(
                    "[barrier] created  step='%s'  parties=%d  targets=%s",
                    step_name,
                    n_targets,
                    [t.id for t in step_targets],
                )

            def _run_step(
                target: Target,
                _action: StageHandler = action,
                _barrier: threading.Barrier | None = barrier,
            ) -> None:
                set_role_log_context(target)
                tname = threading.current_thread().name
                logger.info("[thread] started  thread='%s'  step='%s'", tname, step_name)
                try:
                    ctx = StageContext(
                        target=target,
                        dry_run=_pipeline_config.dry_run,
                        hub_service=self._service_factory.build(target),
                        tcc_config_path=_pipeline_config.tcc_config,
                        tsn_config_path=_pipeline_config.tsn_config,
                        barrier=_barrier,
                    )
                    output = _action(ctx)
                    with lock:
                        stage_output[target.id].extend(output)
                    self._log(f"  Step '{step_name}' [{target.id}] → done")
                    logger.info("Step output [%s]: %s", target.id, output)
                except threading.BrokenBarrierError:
                    # Another thread already recorded a failure and broke the
                    # barrier; add a contextual note but avoid double-reporting.
                    logger.debug(
                        "[barrier] broken-error caught  thread='%s'  step='%s'",
                        threading.current_thread().name,
                        step_name,
                    )
                    with lock:
                        errors.append(
                            f"[{timestamp()}] [{target.id}] Step '{step_name}' aborted: barrier broken by a peer"
                        )
                except Exception as exc:
                    if _barrier is not None:
                        logger.debug(
                            "[barrier] aborting  thread='%s'  step='%s'  reason=%r",
                            threading.current_thread().name,
                            step_name,
                            str(exc),
                        )
                        _barrier.abort()  # unblock any peer waiting at the barrier
                    with lock:
                        errors.append(f"[{timestamp()}] [{target.id}] Step '{step_name}' failed: {exc}")

            threads: list[tuple[threading.Thread, Target]] = []
            for target in step_targets:
                role = target.role or "local"
                th = threading.Thread(
                    target=_run_step,
                    kwargs={"target": target},
                    name=f"{role}/{target.id}",
                    daemon=True,
                )
                threads.append((th, target))
                th.start()
                logger.info("[thread] dispatched  thread='%s'  step='%s'", th.name, step_name)

            for th, target in threads:
                logger.info(
                    "[thread] joining  thread='%s'  step='%s'  timeout=%ss",
                    th.name,
                    step_name,
                    _pipeline_config.timeout,
                )
                th.join(timeout=_pipeline_config.timeout)
                if th.is_alive():
                    logger.debug("[thread] timed out  thread='%s'  step='%s'", th.name, step_name)
                    with lock:
                        errors.append(
                            f"[{timestamp()}] [{target.id}] Step '{step_name}' timed out after {_pipeline_config.timeout}s"
                        )
                else:
                    logger.info("[thread] joined  thread='%s'  step='%s'", th.name, step_name)

            if errors:
                for err in errors:
                    self._errors.append(f"[{timestamp()}] {err}")
                    logger.error(err)
                stage_failed = True
                break

        # Record StageResult per target regardless of success/failure so
        # callers (e.g. live run-stage monitors) can inspect partial output.
        for target in targets:
            target_output = stage_output.get(target.id, [])
            result = StageResult(
                target_id=target.id,
                success=not stage_failed,
                output=target_output,
            )
            self._stage_results.setdefault(target.id, {})[stage_name] = result

        if stage_failed:
            # Dump partial stage results on failure for diagnostics.
            for target in targets:
                sr = self._stage_results.get(target.id, {}).get(stage_name)
                if sr is not None:
                    logger.info(
                        "[stage_results] FAILED  stage='%s'  target='%s'  output=%s",
                        stage_name,
                        target.id,
                        sr.output,
                    )
            return False

        self._log(f"Stage '{stage_name}' → all steps completed")
        for target in targets:
            sr = self._stage_results.get(target.id, {}).get(stage_name)
            if sr is not None:
                logger.info(
                    "[stage_results] OK  stage='%s'  target='%s'  output=%s",
                    stage_name,
                    target.id,
                    sr.output,
                )
        self._log("═" * 60)
        return True


    # -- threaded workflow execution -----------------------------------

    def _execute_workflow(self):
        """Resolve targets, register remote SSH access, then dispatch the stage pipeline.

        Both SINGLE_LOCAL and B2B / MULTI_DUT topologies execute stages
        sequentially through :meth:`_execute_stage_steps`, selecting steps
        from :func:`get_single_target_steps` or :func:`get_multi_target_steps`
        respectively. Aborts on the first stage failure.
        Assumes ``self.config`` is not ``None`` (asserted).

        Workflow::

            _execute_workflow(pipeline_config)
                │
                ├─ resolve_targets()  ──►  list[Target]
                ├─ register_targets()
                │
                ├─ topology_type ∈ {B2B, MULTI_DUT}?
                │       ├─ Yes ──► get_steps = get_multi_target_steps
                │       └─ No  ──► get_steps = get_single_target_steps
                │
                └─ for stage_name in stages_to_run:        ◄─────────────┐
                        │                                                  │
                        ├─ steps = get_steps(stage_name)                  │
                        │                                                  │
                        └─ _execute_stage_steps(stage_name, steps, targets)
                                │                                          │
                                ├─ for each (step_name, roles, action):   │
                                │       │                                  │
                                │       ├─ filter step_targets by role     │
                                │       ├─ Barrier (if n_targets > 1)      │
                                │       │                                  │
                                │       └─ spawn thread per step_target    │
                                │               └─ _run_step(target)       │
                                │                     ├─ set_role_log_context(target)
                                │                     ├─ action(StageContext) → output
                                │                     └─ stage_output[target.id].extend(output)
                                │                                          │
                                ├─ errors? ──► stage_failed=True, break   │
                                │                                          │
                                └─ write StageResult per target            │
                                        └─ _stage_results[target.id][stage_name]
                                                │
                                     True ◄─────┴────► False
                                       │                 │
                               next stage ──────────────►┘  abort pipeline
        """

        if self.pipeline_config is None:
            raise RuntimeError("_execute_workflow requires pipeline_config")

        # Set up targets based on the topology type and register remote targets for SSH access
        targets = assign_target_roles(config=self.pipeline_config)
        connect_remote_targets(targets, log=self._log)

        # Set the pipeline mode (single-target vs multi-DUT) and select the appropriate step retrieval function.
        is_multi_dut = self.pipeline_config.topology_type in (
            DeploymentTopologyType.B2B,
            DeploymentTopologyType.MULTI_DUT,
        )
        get_steps = get_multi_target_steps if is_multi_dut else get_single_target_steps
        mode_label = "Multi-DUT" if is_multi_dut else "Single Target"
        self._log(f"{mode_label} mode: stages={self.pipeline_config.stages_to_run}")

        # Execute stages and their steps sequentially, abort the pipeline if any stage fails.
        for stage_name in self.pipeline_config.stages_to_run:
            target_ids = [f"{t.id}({t.role or 'local'})" for t in targets]
            steps = get_steps(stage_name)

            self._log(f"Stage '{stage_name}' → {len(steps)} step(s) on {target_ids}")
            success = self._execute_stage_steps(stage_name, steps, targets)

            if not success:
                self._log(f"Stage '{stage_name}' failed — aborting pipeline")
                break


    # -- Run Service Requests -----------------------------------

    def _run_pipeline(self) -> ServiceResult:
        """Run the configured deployment workflow and return an aggregated result.

        Assumes ``self.config`` is set; returns a failure result immediately if not.
        Resets internal log and error state on each call — not safe to call
        concurrently on the same instance.

        :return: Aggregated success flag, logs, errors, and per-stage results.
        :rtype: ServiceResult
        """
        if self.pipeline_config is None:
            return ServiceResult(
                success=False,
                logs=[],
                errors=["run() called without a config — use execute(ServiceRequest) or pass config= at construction"],
            )
        try:
            self._log(f"Starting orchestration: topology={self.pipeline_config.topology_type.value}, "
                       f"targets={len(self.pipeline_config.targets)}, "
                       f"stages={self.pipeline_config.stages_to_run}")

            if self.pipeline_config.dry_run:
                self._log("DRY RUN MODE: No changes will be applied")

            self._execute_workflow()

            overall_success = len(self._errors) == 0
            self._log("Orchestration completed" + (" successfully" if overall_success else " with errors"))
            return ServiceResult(
                success=overall_success,
                logs=self._logs,
                errors=self._errors,
                stage_results=self._stage_results or None,
            )

        except Exception as exc:
            logger.exception("Orchestration failed")
            self._errors.append(str(exc))
            return ServiceResult(
                success=False,
                logs=self._logs,
                errors=self._errors,
                stage_results=self._stage_results or None,
            )


    def _run_service_request(self, request: ServiceRequest) -> ServiceResult:
        """Handle a non-pipeline service request via :func:`~.service_handler.handle_service_request`.

        Exceptions are caught, appended to ``self._errors``, and returned as a
        failure result rather than re-raised.

        :param ServiceRequest request: The request to handle.
        :return: Success result with optional *data* payload, or failure result
            on error.
        :rtype: ServiceResult
        """
        svc_label = request.service_type.value if request.service_type else "pipeline"
        self._log(f"[Orchestrator] Received service command: {svc_label}/{request.command.value}")
        cmd_label = f"{svc_label}/{request.command.value}"
        try:
            data = handle_service_request(self._hub_service, request)
            success = data if isinstance(data, bool) else True
            if not success:
                error_msg = f"{cmd_label} reported failure (see daemon logs for details)"
                self._errors.append(error_msg)
                logger.error(error_msg)
            else:
                self._log(f"[Orchestrator][{cmd_label}] command executed.")
            return ServiceResult(
                success=success,
                logs=list(self._logs),
                errors=list(self._errors),
                data=data,
            )
        except Exception as exc:
            logger.exception("Service command [%s] failed", cmd_label)
            self._errors.append(str(exc))
            return ServiceResult(
                success=False,
                logs=list(self._logs),
                errors=list(self._errors),
            )


    # ------------------------------------------------------------------
    # Service-based entry point (CLI → Orchestrator → TimeHubService)
    # ------------------------------------------------------------------

    @property
    def service_manager(self) -> ServiceManager:
        """ServiceManager for daemon lifecycle operations (start, stop, restart, status).

        Request route: CLI → Orchestrator → TimeHubService → ServiceManager

        :rtype: ServiceManager
        """
        return self._hub_service.service_manager

    def execute(self, request: ServiceRequest) -> ServiceResult:
        """Single entry point for all service commands.

        Resets internal log and error state on every call. ``PIPELINE``
        commands require ``request.pipeline_config`` to be set; a failure
        result is returned immediately if it is ``None``.

        :param ServiceRequest request: The command to execute.
        :return: Aggregated result with success flag, logs, errors, and
            optional *data* payload for status/query commands.
        :rtype: ServiceResult
        """
        self._logs = []
        self._errors = []
        self._stage_results = {}

        # PIPELINE command triggers the full workflow pipeline, which is implemented in run() and its helpers.
        if request.command == ServiceCommand.PIPELINE:
            if request.pipeline_config is None:
                return ServiceResult(
                    success=False,
                    logs=[],
                    errors=["PIPELINE command requires pipeline_config to be set on the ServiceRequest"],
                )
            self.pipeline_config = request.pipeline_config
            return self._run_pipeline()

        # All other commands are handled directly by the service handler.
        return self._run_service_request(request)
