from __future__ import annotations

from typing import Any

from skillopt.datasets.base import BatchSpec
from skillopt.envs.base import EnvAdapter

from evoincubation.skillopt_env.dataloader import CanaryDataLoader
from evoincubation.skillopt_env.rollout import run_batch


class CanaryAdapter(EnvAdapter):
    def __init__(
        self,
        *,
        split_dir: str,
        exposure_path: str,
        washout_path: str = "",
        washout_start_epoch: int = 10**9,
        train_size_override: int | None = None,
        workers: int = 1,
        analyst_workers: int = 4,
        failure_only: bool = False,
        minibatch_size: int = 2,
        edit_budget: int = 2,
        seed: int = 42,
        split_seed: int = 42,
        limit: int = 0,
        max_turns: int = 4,
        max_completion_tokens: int = 256,
        exec_timeout: int = 120,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self.workers = int(workers)
        self.analyst_workers = int(analyst_workers)
        self.failure_only = bool(failure_only)
        self.minibatch_size = int(minibatch_size)
        self.edit_budget = int(edit_budget)
        self.max_turns = int(max_turns)
        self.max_completion_tokens = int(max_completion_tokens)
        self.exec_timeout = int(exec_timeout)
        self.dataloader = CanaryDataLoader(
            split_dir=split_dir,
            exposure_path=exposure_path,
            washout_path=washout_path,
            washout_start_epoch=washout_start_epoch,
            train_size_override=train_size_override,
            split_seed=split_seed,
            seed=seed,
            limit=limit,
        )

    def setup(self, cfg: dict) -> None:
        super().setup(cfg)
        self.dataloader.setup(cfg)

    def get_dataloader(self) -> CanaryDataLoader:
        return self.dataloader

    def build_env_from_batch(self, batch: BatchSpec, **kwargs: Any) -> list[dict[str, Any]]:
        del kwargs
        return list(batch.payload or [])

    def build_train_env(self, batch_size: int, seed: int, **kwargs: Any):
        batch = self.dataloader.build_train_batch(batch_size=batch_size, seed=seed, **kwargs)
        return self.build_env_from_batch(batch)

    def build_eval_env(self, env_num: int, split: str, seed: int, **kwargs: Any):
        batch = self.dataloader.build_eval_batch(env_num=env_num, split=split, seed=seed, **kwargs)
        return self.build_env_from_batch(batch)

    def rollout(self, env_manager, skill_content: str, out_dir: str, **kwargs: Any):
        del kwargs
        return run_batch(
            items=list(env_manager),
            skill_content=skill_content,
            out_root=out_dir,
            workers=self.workers,
            max_turns=self.max_turns,
            max_completion_tokens=self.max_completion_tokens,
            timeout=self.exec_timeout,
        )

    def get_task_types(self) -> list[str]:
        return ["budgeted_retrieval"]
