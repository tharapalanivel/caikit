# Copyright The Caikit Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
The LocalModelTrainer uses a local thread to launch and manage each training job
"""

# Standard
from concurrent.futures.thread import _threads_queues
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Type, Union, Callable
import os
import re
import threading
import abc
import uuid

# First Party
import aconfig
import alog

# Local
from ...interfaces.common.data_model.stream_sources import S3Path
from ..data_model import BackgroundStatus
from ..exceptions import error_handler
from ..modules import ModuleBase
from ..toolkit.logging import configure as configure_logging
from .model_trainer_base import ModelTrainerBase, TrainingInfo
from .model_background_base import ModelBackgroundBase, BackgroundInfo, ModelFutureBase
from caikit.core.exceptions.caikit_core_exception import (
    CaikitCoreException,
    CaikitCoreStatusCode,
)
from caikit.core.toolkit.concurrency.destroyable_process import DestroyableProcess
from caikit.core.toolkit.concurrency.destroyable_thread import DestroyableThread
import caikit

log = alog.use_channel("LOC-TRNR")
error = error_handler.get(log)


# 🌶️🌶️🌶️
# Fix for python3.9, 3.10 and 3.11 issue where forked processes always exit with exitcode 1
# when it's created inside a ThreadPoolExecutor: https://github.com/python/cpython/issues/88110
# Fix taken from https://github.com/python/cpython/pull/101940
# Credit: marmarek, https://github.com/marmarek

if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_threads_queues.clear)

class LocalModelFuture(ModelFutureBase):
        """A local model future manages an execution thread for a single train
        operation
        """

        def __init__(
            self,
            future_name: str,
            module_class: Type[ModuleBase],
            save_path: Optional[Union[str, S3Path]],
            save_with_id: bool,
            model_name: Optional[str],
            future_id: Optional[str],
            use_subprocess: bool,
            subprocess_start_method: str,
            args: Iterable[Any],
            kwargs: Dict[str, Any],
        ):
            super().__init__(
                future_name=future_name,
                future_id=future_id or str(uuid.uuid4()),
                save_with_id=save_with_id,
                save_path=save_path,
                model_name=model_name,
                use_reversible_hash=future_id is None,
            )
            self._module_class = module_class

            # Placeholder for the time when the future completed
            self._completion_time = None
            # Set the submission time as right now. (Maybe this should be supplied instead?)
            self._submission_time = datetime.now()

            # Set up the worker and start it
            self._use_subprocess = use_subprocess
            self._subprocess_start_method = subprocess_start_method
            if self._use_subprocess:
                log.debug2("Running training %s as a SUBPROCESS", self.id)
                self._worker = DestroyableProcess(
                    start_method=self._subprocess_start_method,
                    target=self.run,
                    return_result=False,
                    args=args,
                    kwargs={
                        **kwargs,
                    },
                )
                # If training in a subprocess without a save path, the model
                # will be unreachable once trained!
                if not self.save_path:
                    log.warning(
                        "<COR28853922W>",
                        "Training %s launched in a subprocess with no save path",
                        self.id,
                    )
            else:
                log.debug2("Running training %s as a THREAD", self.id)
                self._worker = DestroyableThread(
                    self.run,
                    *args,
                    **kwargs,
                )
            self._worker.start()

        @property
        def completion_time(self) -> Optional[datetime]:
            return self._completion_time

        ## Interface ##

        def get_info(self) -> BackgroundInfo:
            """Every model future must be able to poll the status of the
            training job
            """

            # The worker was canceled while doing work. It may still be in the
            # process of terminating and thus still alive.
            if self._worker.canceled:
                return self._make_background_info(status=BackgroundStatus.CANCELED)

            # If the worker is currently alive it's doing work
            if self._worker.is_alive():
                return self._make_background_info(status=BackgroundStatus.RUNNING)

            # The worker threw outside of a cancellation process
            if self._worker.threw:
                return self._make_background_info(
                    status=BackgroundStatus.ERRORED, errors=[self._worker.error]
                )

            # The worker completed its work without being canceled or raising
            if self._worker.ran:
                return self._make_background_info(status=BackgroundStatus.COMPLETED)

            # If it's not alive and not done, it hasn't started yet
            return self._make_background_info(status=BackgroundStatus.QUEUED)

        def run(self):
            raise NotImplementedError("Subclasses of LocalModelFuture must implement a run method")
        
        def cancel(self):
            """Terminate the given training"""
            log.debug("Canceling training %s", self.id)
            with alog.ContextTimer(
                log.debug2, "Done canceling training %s in: ", self.id
            ):
                log.debug3("Destroying worker in %s", self.id)
                self._worker.destroy()

        def wait(self):
            """Block until the job reaches a terminal state"""
            log.debug2("Waiting for %s", self.id)
            self._worker.join()
            log.debug2("Done waiting for %s", self.id)
            self._completion_time = self._completion_time or datetime.now()

        @abc.abstractmethod
        def load(self) -> ModuleBase:
            """Load the resultant model into memory. For trainer subclasses this
            is the trained model while inferencer subclasses return the existing model
            """
            

        ## Impl ##
        def _make_background_info(
            self, status: BackgroundStatus, errors: Optional[List[Exception]] = None
        ) -> BackgroundInfo:
            return BackgroundInfo(
                status=status,
                errors=errors,
                completion_time=self._completion_time,
                submission_time=self._submission_time,
            )

class LocalModelBackground(ModelBackgroundBase):
    __doc__ = __doc__

    ## Interface ##

    # Expression for parsing retention policy
    _timedelta_expr = re.compile(
        r"^((?P<days>\d+?)d)?((?P<hours>\d+?)h)?((?P<minutes>\d+?)m)?((?P<seconds>\d*\.?\d*?)s)?$"
    )

    def __init__(self, config: aconfig.Config, instance_name: str):
        """Initialize with a shared dict of all trainings"""
        self._instance_name = instance_name
        self._use_subprocess = config.get("use_subprocess", False)
        self._subprocess_start_method = config.get("subprocess_start_method", "spawn")
        self._retention_duration = config.get("retention_duration")
        if self._retention_duration is not None:
            try:
                log.debug2("Parsing retention duration: %s", self._retention_duration)
                self._retention_duration = timedelta(
                    **{
                        key: float(val)
                        for key, val in self._timedelta_expr.match(
                            self._retention_duration
                        )
                        .groupdict()
                        .items()
                        if val is not None
                    }
                )
            except AttributeError:
                error(
                    "<COR63897671E>",
                    ValueError(
                        f"Invalid retention_duration: {self._retention_duration}"
                    ),
                )

        # The shared dict of futures and a lock to serialize mutations to it
        self._futures = {}
        self._futures_lock = threading.Lock()

    def get_model_future(self, training_id: str) -> "LocalModelFuture":
        """Look up the model future for the given id"""
        self._purge_old_futures()
        if model_future := self._futures.get(training_id):
            return model_future
        raise CaikitCoreException(
            status_code=CaikitCoreStatusCode.NOT_FOUND,
            message=f"Unknown training_id: {training_id}",
        )

    ## Impl ##

    def _purge_old_futures(self):
        """If a retention duration is configured, purge any futures that are
        older than the policy
        """
        if self._retention_duration is None:
            return
        now = datetime.now()
        purged_ids = {
            fid
            for fid, future in self._futures.items()
            if future.completion_time is not None
            and future.completion_time + self._retention_duration < now
        }
        if not purged_ids:
            log.debug3("No ids to purge")
            return
        log.debug3("Purging ids: %s", purged_ids)
        with self._futures_lock:
            for fid in purged_ids:
                # NOTE: Concurrent purges could have already done this, so don't
                #   error if the id is already gone
                self._futures.pop(fid, None)

