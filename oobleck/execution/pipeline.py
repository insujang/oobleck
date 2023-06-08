from __future__ import annotations

import torch
import torch.distributed
import itertools

from types import MethodType
from typing import Union, Any, Dict, Mapping, Tuple, List, Optional, Iterable

from torch.distributed import ProcessGroup, Work
from deepspeed import comm as dist
from deepspeed.utils import logger, instrument_w_nvtx
from deepspeed.runtime.pipe import schedule
from deepspeed.ops.adam import FusedAdam
from deepspeed.runtime.lr_schedules import WarmupLR

from oobleck.execution.dataloader import OobleckDataLoader
from oobleck.module.model import OobleckModel
from oobleck.module.layer import is_checkpointable
from oobleck.execution.utils import (
    zero_grads,
    DTYPE_TO_ID,
    ID_TO_DTYPE,
)
from oobleck.csrc.planning.pipeline_template import PipelineTemplate
from oobleck.utils.timer import OobleckTimer, measure_time

from transformers import TrainingArguments


# Applied patch https://github.com/microsoft/DeepSpeed/pull/2862
# for odd number of stages pipeline.
class OobleckPipelineSchedule(schedule.TrainSchedule):
    """A schedule for training a batch using pipeline parallelism.

    Unlike existing :class:`deepspeed.runtime.pipe.schedule.TrainSchedule`,
    :class:`OobleckPipelineSchedule` decouples allreduce synchronization and optimizer step
    from pipeline execution and only schedules computation part and intermediate p2p operations.

    reducing (tied) gradients and optimizer step must be done separately.
    """

    def steps(self):
        prev_micro_batch_id = -1
        total_steps = 2 * (self.micro_batches + self.stages - 1)
        for step_id in range(total_steps):
            micro_batch_id, is_forward = self._step_to_micro_batch(step_id)

            if self._valid_micro_batch(prev_micro_batch_id):
                prev_buffer = self._buffer_idx(prev_micro_batch_id)
            if self._valid_micro_batch(micro_batch_id):
                curr_buffer = self._buffer_idx(micro_batch_id)

            cmds = []

            # Exchange activations
            if is_forward:
                if self._valid_micro_batch(prev_micro_batch_id) and self._valid_stage(
                    self.prev_stage
                ):
                    cmds.append(schedule.SendGrad(prev_buffer))
                if self._valid_micro_batch(micro_batch_id) and self._valid_stage(
                    self.prev_stage
                ):
                    cmds.append(schedule.RecvActivation(curr_buffer))

            else:
                if self._valid_micro_batch(micro_batch_id) and self._valid_stage(
                    self.next_stage
                ):
                    cmds.append(schedule.RecvGrad(curr_buffer))
                if self._valid_micro_batch(prev_micro_batch_id) and self._valid_stage(
                    self.next_stage
                ):
                    cmds.append(schedule.SendActivation(prev_buffer))

            # First/last stage loads
            if self.stage_id == 0 or self.stage_id == self.stages - 1:
                if is_forward and self._valid_micro_batch(micro_batch_id):
                    cmds.append(schedule.LoadMicroBatch(curr_buffer))

            # Computation
            if self._valid_micro_batch(micro_batch_id):
                if is_forward:
                    cmds.append(schedule.ForwardPass(curr_buffer))
                else:
                    cmds.append(schedule.BackwardPass(curr_buffer))

            # No reduce and optimizer step here at the end of the batch

            # Prepare state for next time
            prev_micro_batch_id = micro_batch_id
            yield cmds

    def num_pipe_buffers(self):
        """Return the number of pipeline buffers required for this stage.
        This is equivalent to the maximum number of in-flight forward passes,
        since we need to remember the activations of forward passes in order
        to run backpropagation. For synchronous 1F1B, this is equivalent to
        the index difference between this stage and the last stage.
        """
        buffers = min(self.stages - self.stage_id, self.micro_batches)
        return max(2, buffers)


class PipelineExecution:
    def __init__(
        self,
        pipeline: OobleckPipeline,
        training_args: TrainingArguments,
        dataloader: OobleckDataLoader,
    ):
        self.pipeline = pipeline
        self.training_args = training_args
        self.dataloader = dataloader
        self.device = torch.device("cuda")

        self.reset_data_iterator()

        # store checkpointability for each layer
        for layer in self.pipeline.model_layers:
            layer.set_checkpointable(is_checkpointable(layer))

        # stores the loss for the current microbatch being processed
        self.loss: Optional[Union[torch.Tensor, Iterable[torch.Tensor]]] = None

        # stores the loss for the entire batch
        self.total_loss: Optional[Union[torch.Tensor, Iterable[torch.Tensor]]] = None

        self.micro_steps = 0

        # TODO: use HF arguments to initialize optimizer and LR properly
        parameters = list(
            itertools.chain(
                *[list(layer.parameters()) for layer in self.pipeline.model_layers]
            )
        )
        self.optimizer = FusedAdam(
            parameters,
            self.training_args.learning_rate,
            betas=(self.training_args.adam_beta1, self.training_args.adam_beta2),
            eps=self.training_args.adam_epsilon,
            adam_w_mode=True,
        )
        num_training_steps = len(self.dataloader)
        self.lr_scheduler = WarmupLR(
            self.optimizer, self.training_args.get_warmup_steps(num_training_steps)
        )

    def reset_data_iterator(self):
        self.data_iterator = iter(self.dataloader)

    # https://github.com/huggingface/transformers/blob/v4.26.1/src/transformers/trainer.py#L2454
    def _prepare_input(
        self, data: Union[torch.Tensor, Any]
    ) -> Union[torch.Tensor, Any]:
        """
        Prepares one `data` before feeding it to the model, be it a tensor or a nested list/dictionary of tensors.
        """
        if isinstance(data, Mapping):
            return type(data)({k: self._prepare_input(v) for k, v in data.items()})
        elif isinstance(data, (tuple, list)):
            return type(data)(self._prepare_input(v) for v in data)
        elif isinstance(data, torch.Tensor):
            data = data.clone().detach().to(self.device)
            data.requires_grad = data.is_floating_point()
            return data
        return data

    # https://github.com/huggingface/transformers/blob/v4.26.1/src/transformers/trainer.py#L2472
    def _prepare_inputs(
        self, inputs: Dict[str, Union[torch.Tensor, Any]]
    ) -> Tuple[Union[torch.Tensor, Any]]:
        """
        Prepare `inputs` before feeding them to the model, converting them to tensors if they are not already and
        handling potential state.
        """
        return tuple(self._prepare_input(t) for _, t in inputs.items())

    @instrument_w_nvtx
    @measure_time("execution/load_microbatch")
    def load_microbatch(self, buffer_id: int):
        assert (
            self.pipeline.is_first_stage() or self.pipeline.is_last_stage()
        ), "load_microatch can only be called at either the first stage or the last stage."

        if self.pipeline.is_first_stage():
            batch = next(self.data_iterator)
            self.pipeline.pipe_buffers["inputs"][buffer_id] = self._prepare_inputs(
                batch
            )

    @instrument_w_nvtx
    @measure_time("execution/forward")
    def forward_pass(self, buffer_id: int):
        inputs: tuple[torch.Tensor] = self.pipeline.pipe_buffers["inputs"][buffer_id]
        zero_grads(inputs)

        # XXX Hack
        # Some tensor might be converted from torch.Size().
        # Convert it to torch.Size so that forward can be executed
        inputs: tuple[Union[torch.Size, torch.Tensor]] = tuple(
            [
                torch.Size(input.tolist())
                if input.dim() == 1
                and input.data[0] == self.training_args.per_device_train_batch_size
                else input
                for input in inputs
            ]
        )

        # Execute forward
        for layer in self.pipeline.model_layers:
            inputs = layer(*inputs)

        outputs = inputs

        # Optionally compute loss on the last stage
        if self.pipeline.is_last_stage():
            self.loss = outputs["loss"]
            del outputs["logits"]

            if isinstance(self.loss, torch.Tensor):
                if self.total_loss is None:
                    self.total_loss = torch.zeros_like(self.loss)
                self.total_loss += self.loss.detach()
            else:
                if self.total_loss is None:
                    self.total_loss = [torch.zeros_like(l) for l in self.loss]
                for idx, l in enumerate(self.loss):
                    assert torch.is_tensor(l)
                    self.total_loss[idx] += l.detach()
        else:
            # XXX Hack
            # It might includes torch.Size() in outputs.
            # Convert it to torch.Tensor so that it can be transferred
            outputs: tuple[torch.Tensor] = tuple(
                [
                    output
                    if torch.is_tensor(output)
                    else torch.LongTensor(data=output).to(self.device)
                    for output in outputs
                ]
            )

            self.pipeline.pipe_buffers["outputs"][buffer_id] = outputs

    @instrument_w_nvtx
    @measure_time("execution/backward")
    def backward_pass(self, buffer_id: int):
        if self.pipeline.is_last_stage():
            loss = self.loss
            loss.backward()
        else:
            output_tensors: Tuple[torch.Tensor] = self.pipeline.pipe_buffers["outputs"][
                buffer_id
            ]
            output_tensors = tuple([t for t in output_tensors if t.requires_grad])
            grad_tensors: Tuple[
                torch.Tensor
            ] = self.pipeline.communication.grad_recv_buf

            # Oobleck sharded model always returns tuple with tensors and torch.Size.
            assert len(output_tensors) == len(grad_tensors)
            torch.autograd.backward(tensors=output_tensors, grad_tensors=grad_tensors)

        # Free up memory from the output of forward()
        self.pipeline.pipe_buffers["outputs"][buffer_id] = None
        grad_tensors = None

    @instrument_w_nvtx
    @measure_time("execution/step")
    def optimizer_step(self, lr_kwargs=None):
        # amp enable check: gradient clipping
        self.optimizer.step()

        overflow = (
            self.optimizer.overflow if hasattr(self.optimizer, "overflow") else False
        )
        if not overflow:
            self.lr_scheduler.step(**(lr_kwargs or {}))


class PipelineCommunication:
    def __init__(
        self,
        pipeline: OobleckPipeline,
        process_group: ProcessGroup,
    ):
        self.pipeline = pipeline
        self.device = torch.device("cuda")
        self.process_group = process_group

        self.sent_activation_meta: bool = False
        # initialized in :func:`oobleck.execution.PipelineCommunication.recv_activations`.
        self.activation_recv_buf: Optional[Tuple[torch.Tensor]] = None
        # initialized in :func:`oobleck.execution.PipelineCommunication.recv_gradients`.
        self.grad_recv_buf: Optional[Tuple[torch.Tensor]] = None

    def _send(
        self, tensor: torch.Tensor, dest_rank: int, async_op: bool = False
    ) -> Work:
        return (
            dist.isend(tensor, dest_rank, self.process_group)
            if async_op
            else dist.send(tensor, dest_rank, self.process_group)
        )

    def _recv(
        self, tensor: torch.Tensor, src_rank: int, async_op: bool = False
    ) -> Work:
        return (
            dist.irecv(tensor, src_rank, self.process_group)
            if async_op
            else dist.recv(tensor, src_rank, self.process_group)
        )

    @measure_time("comm/send_activations")
    def send_activations(self, buffer_id: int):
        def _send_activation_meta(buffer: Tuple[torch.Tensor], receiver_rank: int):
            """Send activation dimension first to the next stage
            so that it can initialize buffers.

            Metadata is communicated in this order:
                * num_tensors in tensor tuple
                foreeach tensor in buffer:
                    * ndims
                    * dtype
                    * shape
                    * requires_grad
            """
            assert isinstance(
                buffer, tuple
            ), f"Could not send meta type {type(buffer)}."
            count_tensor = torch.LongTensor(data=[len(buffer)]).to(self.device)
            self._send(count_tensor, receiver_rank)
            for tensor in buffer:
                assert isinstance(tensor, torch.Tensor)
                send_ndims = torch.LongTensor(data=[len(tensor.size())]).to(self.device)
                send_dtype = torch.LongTensor(data=[DTYPE_TO_ID[tensor.dtype]]).to(
                    self.device
                )
                send_shape = torch.LongTensor(data=tensor.size()).to(self.device)
                send_req_grad = torch.LongTensor(
                    data=[1 if tensor.requires_grad else 0]
                ).to(self.device)
                self._send(send_ndims, receiver_rank)
                self._send(send_dtype, receiver_rank)
                self._send(send_shape, receiver_rank)
                self._send(send_req_grad, receiver_rank)

        outputs: Tuple[torch.Tensor] = self.pipeline.pipe_buffers["outputs"][buffer_id]
        if not self.sent_activation_meta:
            _send_activation_meta(outputs, self.pipeline.next_rank)
            self.sent_activation_meta = True

        assert isinstance(outputs, tuple)
        for buffer in outputs:
            assert isinstance(buffer, torch.Tensor)
            self._send(buffer, self.pipeline.next_rank)

    @measure_time("comm/recv_activations")
    def recv_activations(self, buffer_id: int):
        def create_receive_buffer(sender_rank: int) -> Tuple[torch.Tensor]:
            """Receive metadata about upcoming p2p transfers and return allocated buffer.

            Metadata is communicated in this order:
                * num_tensors in tensor tuple
                foreeach tensor in buffer:
                    * ndims
                    * dtype
                    * shape
                    * requires_grad
            """
            count_tensor = torch.LongTensor(data=[0]).to(self.device)
            self._recv(count_tensor, sender_rank)
            num_tensors = count_tensor.item()
            buffers: List[torch.Tensor] = []
            for _ in range(num_tensors):
                recv_ndims = torch.LongTensor(data=[0]).to(self.device)
                self._recv(recv_ndims, sender_rank)
                recv_ndims = recv_ndims.item()

                recv_dtype = torch.LongTensor(data=[0]).to(self.device)
                self._recv(recv_dtype, sender_rank)
                recv_dtype = ID_TO_DTYPE[recv_dtype.item()]

                recv_shape = torch.LongTensor([1] * recv_ndims).to(self.device)
                self._recv(recv_shape, sender_rank)
                recv_shape = recv_shape.tolist()

                recv_req_grad = torch.LongTensor(data=[0]).to(self.device)
                self._recv(recv_req_grad, sender_rank)
                recv_req_grad = True if recv_req_grad.item() == 1 else False

                buffers.append(
                    torch.zeros(
                        recv_shape,
                        device=self.device,
                        dtype=recv_dtype,
                        requires_grad=recv_req_grad,
                    )
                )
            return tuple(buffers)

        if self.activation_recv_buf is None:
            self.activation_recv_buf = create_receive_buffer(self.pipeline.prev_rank)

        assert isinstance(self.activation_recv_buf, tuple)
        recvd: List[Optional[torch.Tensor]] = [None] * len(self.activation_recv_buf)
        for idx, buffer in enumerate(self.activation_recv_buf):
            assert torch.is_tensor(buffer)
            self._recv(buffer, self.pipeline.prev_rank)
            recvd[idx] = buffer.clone().detach()
            recvd[idx].requires_grad = buffer.requires_grad

        self.pipeline.pipe_buffers["inputs"][buffer_id] = tuple(recvd)

    @measure_time("comm/send_gradients")
    def send_gradients(self, buffer_id: int):
        inputs = self.pipeline.pipe_buffers["inputs"][buffer_id]
        assert isinstance(inputs, tuple)

        for buffer in inputs:
            # Skip tensors that will not produce a gradient
            if not buffer.requires_grad:
                assert buffer.grad is None
                continue
            assert buffer.grad is not None
            self._send(buffer.grad, self.pipeline.prev_rank)

        # We can free up the input buffer now
        self.pipeline.pipe_buffers["inputs"][buffer_id] = None

    @measure_time("comm/recv_gradients")
    def recv_gradients(self, buffer_id: int):
        def create_gradients_buffer(
            tensors: Tuple[torch.Tensor],
        ) -> Tuple[torch.Tensor]:
            assert isinstance(tensors, tuple)
            buffers: List[torch.Tensor] = []
            for tensor in tensors:
                assert isinstance(tensor, torch.Tensor)
                if tensor.requires_grad:
                    buffers.append(torch.zeros_like(tensor))

            return tuple(buffers)

        outputs = self.pipeline.pipe_buffers["outputs"][buffer_id]
        assert isinstance(outputs, tuple)

        # Allocate gradients if necessary
        if self.grad_recv_buf is None:
            self.grad_recv_buf = create_gradients_buffer(outputs)

        for buffer in self.grad_recv_buf:
            self._recv(buffer, self.pipeline.next_rank)


# A map of PipeInstruction types to methods. Each method will be executed with the
# kwargs provided to the PipeInstruction from the scheduler.
INSTRUCTION_MAP = {
    schedule.OptimizerStep: PipelineExecution.optimizer_step,
    schedule.LoadMicroBatch: PipelineExecution.load_microbatch,
    schedule.ForwardPass: PipelineExecution.forward_pass,
    schedule.BackwardPass: PipelineExecution.backward_pass,
    schedule.SendActivation: PipelineCommunication.send_activations,
    schedule.RecvActivation: PipelineCommunication.recv_activations,
    schedule.SendGrad: PipelineCommunication.send_gradients,
    schedule.RecvGrad: PipelineCommunication.recv_gradients,
}


# FIXME: all ranks now should be changed to the global rank.
# TODO: Integrate FSDP.
# NOTE: it is going to be completely recreated during reconfiguration after failure.
class OobleckPipeline:
    def __init__(
        self,
        pipeline_template: PipelineTemplate,
        model: OobleckModel,
        dataloader: OobleckDataLoader,
        step: int,
        ranks: List[int],
        process_group: ProcessGroup,
        training_args: TrainingArguments,
    ):
        logger.info(f"Creating pipeline ({len(pipeline_template.get_stages())} stages)")

        assert dist.is_initialized(), "torch.distributed is not intialized."
        num_gpus_per_node = pipeline_template.get_num_gpus_per_node()
        assert (
            len(ranks) == len(pipeline_template.get_stages()) * num_gpus_per_node
        ), "Number of ranks must be equal to number of stages * num_gpus_per_node."
        self.ranks = ranks
        self.my_rank: dist.get_rank()
        assert self.my_rank in self.ranks, "My rank is not in the ranks list."

        rank_index = ranks.index(self.my_rank)
        self.prev_rank: Optional[int] = (
            ranks[rank_index - num_gpus_per_node]
            if rank_index >= num_gpus_per_node
            else None
        )
        self.next_rank: Optional[int] = (
            ranks[rank_index + num_gpus_per_node]
            if rank_index < len(ranks) - num_gpus_per_node
            else None
        )

        self.total_num_layers = len(model.model)
        # Find the stage I am responsible to execute
        for stage_index, stage in enumerate(pipeline_template.get_stages()):
            layer_indices = stage.get_layer_indices()
            if self.my_rank not in range(layer_indices[0], layer_indices[1]):
                continue

            self.model_layers = [
                layer.to("cuda")
                for layer in model.model[layer_indices[0] : layer_indices[1]]
            ]
            self.my_stage_index = stage_index
            break
        assert self.model_layers, "Could not find a stage to execute."

        self.train_schedule = OobleckPipelineSchedule(
            self.execution.dataloader.num_my_microbatches,
            len(pipeline_template.get_stages()),
            stage_index,
        )

        num_pipe_buffers = self.train_schedule.num_pipe_buffers()
        self.pipe_buffers: Dict[str, Tuple[torch.Tensor]] = {
            # batch input and received activations
            "inputs": [None for _ in range(num_pipe_buffers)],
            # labels from batch input
            "labels": [None for _ in range(num_pipe_buffers)],
            # activations to be sent
            "outputs": [None for _ in range(num_pipe_buffers)],
        }

        self.communication = PipelineCommunication(self, process_group)
        self.execution = PipelineExecution(self, training_args, dataloader)

        self.timer = OobleckTimer()
        self.global_steps = step

    def write_samples_logs(self):
        lr = next(
            iter(
                [
                    param_group["lr"]
                    for param_group in self.execution.optimizer.param_groups
                    if "lr" in param_group
                ]
            ),
            0.0,
        )
        loss = self.execution.total_loss.mean().item() if self.is_last_stage() else -1
        self.execution.total_loss = None

        self.timer.write_events([(f"samples/lr", lr, self.global_steps)])
        self.timer.write_events([(f"samples/train_loss", loss, self.global_steps)])

    def train(self):
        for step_cmds in self.train_schedule:
            # For each instruction in the step
            for cmd in step_cmds:
                if type(cmd) not in INSTRUCTION_MAP:
                    raise RuntimeError(
                        f"{self.__class__.__name__} does not understand instruction {repr(cmd)}"
                    )

                # Equivalent to: self._exec_forward_pass(buffer_id=0)
                _exec_instr = MethodType(INSTRUCTION_MAP[type(cmd)], self)
                _exec_instr(**cmd.kwargs)

        self.global_steps += 1
        self.write_samples_logs()

    def is_first_stage(self):
        return self.model_layers[0].index == 0

    def is_last_stage(self):
        return self.model_layers[-1].index == self.total_num_layers - 1
