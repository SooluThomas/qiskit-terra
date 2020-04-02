# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2020.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Context based pulse programming interface.

Use the context builder interface to program pulse programs with assembly-like
syntax. For example::

.. code::
    from qiskit.circuit import QuantumCircuit
    from qiskit.pulse import pulse_lib, Schedule, Gaussian, DriveChannel
    from qiskit.test.mock import FakeOpenPulse2Q

    sched = Schedule()

    # This creates a PulseProgramBuilderContext(sched, backend=backend)
    # instance internally and wraps in a
    # dsl builder context
    with builder(sched, backend=backend):
      # create a pulse
      gaussian_pulse = pulse_lib.gaussian(10, 1.0, 2)
      # create a channel type
      d0 = DriveChannel(0)
      d1 = DriveChannel(1)
      # play a pulse at time=0
      play(d0, gaussian_pulse)
      # play another pulse directly after at t=10
      play(d0, gaussian_pulse)
      # The default scheduling behavior is to schedule pulse in parallel
      # across independent resources, for example
      # play the same pulse on a different channel at t=0
      play(d1, gaussian_pulse)

      # We also provide alignment contexts
      # if channels are not supplied defaults to all channels
      # this context starts at t=10 due to earlier pulses
      with sequential():
        play(d0, gaussian_pulse)
        # play another pulse after at t=20
        play(d1, gaussian_pulse)

        # we can also layer contexts as each instruction is contained in its
        # local scheduling context (block).
        # Scheduling contexts are layered, and the output of a child context is
        # a fixed scheduled block in its parent context.
        # starts at t=20
        with parallel():
          # start at t=20
          play(d0, gaussian_pulse)
          # start at t=20
          play(d1, gaussian_pulse)
        # context ends at t=30

      # We also support different alignment contexts
      # Default is
      # with left():

      # all pulse instructions occur as late as possible
      with right_align():
        set_phase(d1, math.pi)
        # starts at t=30
        delay(d0, 100)
        # ends at t=130

        # starts at t=120
        play(d1, gaussian_pulse)
        # ends at t=130

      # acquire a qubit
      acquire(0, ClassicalRegister(0))
      # maps to
      #acquire(AcquireChannel(0), ClassicalRegister(0))

      # We will also support a variety of helper functions for common operations

      # measure all qubits
      # Note that as this DSL is pure Python
      # any Python code is accepted within contexts
      for i in range(n_qubits):
        measure(i, ClassicalRegister(i))

      # delay on a qubit
      # this requires knowledge of which channels belong to which qubits
      delay(0, 100)

      # insert a quantum circuit. This functions by behind the scenes calling
      # the scheduler on the given quantum circuit to output a new schedule
      # NOTE: assumes quantum registers correspond to physical qubit indices
      qc = QuantumCircuit(2, 2)
      qc.cx(0, 1)
      call(qc)
      # We will also support a small set of standard gates
      u3(0, 0, np.pi, 0)
      cx(0, 1)


      # It is also be possible to call a preexisting
      # schedule constructed with another
      # NOTE: once internals are fleshed out, Schedule may not be the default class
      tmp_sched = Schedule()
      tmp_sched += Play(dc0, gaussian_pulse)
      call(tmp_sched)

      # We also support:

      # frequency instructions
      set_frequency(dc0, 5.0e9)
      shift_frequency(dc0, 0.1e9)

      # phase instructions
      set_phase(dc0, math.pi)
      shift_phase(dc0, 0.1)

      # offset contexts
      with phase_offset(d0, math.pi):
        play(d0, gaussian_pulse)

      with frequency_offset(d0, 0.1e9):
        play(d0, gaussian_pulse)
"""
import collections
import contextvars
import functools
from contextlib import contextmanager
from typing import Any, Callable, Dict, Union

from qiskit.extensions.standard import (CnotGate, U1Gate, U2Gate, U3Gate, XGate)
from qiskit.circuit import QuantumCircuit
from qiskit.compiler import transpile

from . import Pulse, PulseError, transforms, macros
from .channels import (AcquireChannel, Channel, MemorySlot,
                       PulseChannel, RegisterSlot)
from .circuit_scheduler import schedule_circuit
from .configuration import Discriminator, Kernel
from .instructions import (Acquire, Delay, Instruction, Play,
                           SetFrequency, ShiftPhase, Snapshot)
from .schedule import Schedule


#: contextvars.ContextVar[BuilderContext]: current builder
BUILDER_CONTEXT = contextvars.ContextVar("backend")


class PulseBuilderContext():
    """Builder context class."""

    def __init__(self, backend, block: Schedule = None):
        """Initialize builder context.

        TODO: This should contain a builder class rather than manipulating the
        IR directly.

        Args:
            backend (BaseBackend):
        """

        #: BaseBackend: Backend instance for context builder.
        self.backend = backend

        #: Schedule: Current current schedule of BuilderContext.
        self.block = None

        if block is None:
            block = Schedule()

        self.set_current_block(block)

        #: Set[Schedule]: Collection of all builder blocks.
        self.blocks = set()

        #: QuantumCircuit: Lazily constructed quantum circuit
        self._lazy_circuit = self.new_circuit()
        #: Schedule: Final Schedule program block.
        self._program = block

    def __enter__(self):
        """Enter Builder Context."""
        self._backend_ctx_token = BUILDER_CONTEXT.set(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit Builder Context."""
        BUILDER_CONTEXT.reset(self._backend_ctx_token)

    @property
    def num_qubits(self):
        """Get the number of qubits in the backend."""
        return self.backend.configuration().num_qubits

    def new_circuit(self):
        """Create a new circuit for scheduling."""
        return QuantumCircuit(self.num_qubits)

    def set_current_block(self, block: Schedule) -> Schedule:
        """Set the current block."""
        assert isinstance(block, Schedule)
        self.block = block
        return block

    def _schedule_lazy_circuit_before(self, fn):
        """Decorator thats schedules and calls the active circuit executing
        the decorated function."""
        @functools.wraps(fn)
        def wrapper(*args, **kwds):
            self._schedule_lazy_circuit()
            return fn(*args, **kwds)
        return wrapper

    @_schedule_lazy_circuit_before
    def append_block(self, block: Schedule):
        """Add a block to the current active block."""
        self.current_block.append(block)

    @_schedule_lazy_circuit_before
    def append_instruction(self, instruction: Instruction):
        """Add an instruction to the current active block."""
        self.current_block.append(instruction)

    @_schedule_lazy_circuit_before
    def call_schedule(self, schedule: Schedule):
        """Call a schedule."""
        self.append_block(schedule)

    def _schedule_lazy_circuit(self):
        """Call a QuantumCircuit."""
        if len(self._lazy_circuit):
            circuit = transpile(self.lazy_circuit,
                                self.backend,
                                **self.transpiler_settings)
            sched = schedule_circuit(circuit,
                                     self.backend,
                                     **self.circuit_scheduler_settings)
            self.call_schedule(sched)
            # reset active circuit
            self._lazy_circuit = self.new_circuit()

    def call_circuit(self, circuit: QuantumCircuit, lazy=True):
        self._lazy_circuit.extend(circuit)
        if not lazy:
            self._schedule_lazy_circuit()

    def compile(self) -> Schedule:
        """Compile final pulse schedule program."""
        return self._program


def build(backend, schedule):
    """
    A context manager for the pulse DSL.

    Args:
        backend: a qiskit backend
        schedule: a *mutable* pulse Schedule
    """
    return PulseBuilderContext(backend, schedule)


# Builder Utilities ############################################################
def active_builder() -> PulseBuilderContext:
    """Get the active builder in the current context."""
    return BUILDER_CONTEXT.get()


def active_backend():
    """Get the backend of the current context.

    Returns:
        BaseBackend
    """
    return active_builder().backend


def append_block(block: Schedule):
    """Append a block to the current block. The current block is not changed."""
    active_builder().append_block(block)


def append_instruction(instruction: Instruction):
    """Append an instruction to current context."""
    active_builder().append_instruction(instruction)


def qubit_channels(qubit: int):
    """
    Returns the 'typical' set of channels associated with a qubit.
    """
    raise NotImplementedError('Qubit channels is not yet implemented.')


# Transform Contexts ###########################################################
def _transform_context(transform: Callable) -> Callable:
    """A tranform context.

    Args:
        transform: Transform to decorate as context.
    """
    @functools.wraps(transform)
    def wrap(fn):
        @contextmanager
        def wrapped_transform(blocks, *args, **kwargs):
            builder = active_builder()
            block = builder.set_current_block(Schedule())
            try:
                yield
            finally:
                builder.set_current_block(transform(block, *args, **kwargs))

        return wrapped_transform

    return wrap


@_transform_context(transforms.left_barrier)
def left_barrier():
    """Left barrier transform builder context."""


@_transform_context(transforms.right_barrier)
def right_barrier():
    """Right barrier transform builder context."""


@_transform_context(transforms.left_align)
def left_align():
    """Left align transform builder context."""


@_transform_context(transforms.right_align)
def right_align():
    """Right align transform builder context."""


@_transform_context(transforms.sequentialize)
def sequential():
    """Sequential transform builder context."""


@_transform_context(transforms.parallelize)
def parallel():
    """Parallel transform builder context."""


@_transform_context(transforms.group)
def group():
    """Group the instructions within this context fixing their relative timing."""


@_transform_context(transforms.flatten)
def flatten():
    """Flatten any grouped instructions upon exiting context."""


# Compiler Directive Contexts ##################################################
@contextmanager
def transpiler_settings(**settings):
    """Set the current active tranpiler settings for this context."""
    builder = active_builder()
    transpiler_settings = builder.transpiler_settings
    builder.transpiler_settings = collections.ChainMap(
        settings, transpiler_settings)
    try:
        yield
    finally:
        builder.transpiler_settings = transpiler_settings


@contextmanager
def circuit_scheduling_settings(**settings):
    """Set the current active circuit scheduling settings for this context."""
    builder = active_builder()
    circuit_scheduler_settings = builder.circuit_scheduler_settings
    builder.circuit_scheduler_settings = collections.ChainMap(
        settings, circuit_scheduler_settings)
    try:
        yield
    finally:
        builder.circuit_scheduler_settings = circuit_scheduler_settings


def active_transpiler_settings() -> Dict[str, Any]:
    """Return current context transpiler settings."""


def active_circuit_scheduler_settings() -> Dict[str, Any]:
    """Return current context circuit scheduler settings."""


# Base Instructions ############################################################
def delay(channel: Channel, duration: int):
    """Delay on a ``channel`` for a ``duration``."""
    append_instruction(Delay(duration, channel))


def play(channel: PulseChannel, pulse: Pulse):
    """Play a ``pulse`` on a ``channel``."""
    append_instruction(Play(pulse, channel))


def acquire(channel: Union[AcquireChannel, int],
            register: Union[RegisterSlot, MemorySlot],
            duration: int,
            **metadata: Union[Kernel, Discriminator]):
    """Acquire for a ``duration`` on a ``channel`` and store the result in a ``register``."""
    if isinstance(register, MemorySlot):
        append_instruction(Acquire(duration, channel, mem_slot=register, **metadata))
    elif isinstance(register, RegisterSlot):
        append_instruction(Acquire(duration, channel, reg_slot=register, **metadata))
    raise PulseError(
        'Register of type: "{}" is not supported'.format(type(register)))


def set_frequency(channel: PulseChannel, frequency: float):
    """Set the ``frequency`` of a pulse ``channel``."""
    append_instruction(SetFrequency(frequency, channel))


def shift_frequency(channel: PulseChannel, frequency: float):
    """Shift the ``frequency`` of a pulse ``channel``."""
    raise NotImplementedError()


def set_phase(channel: PulseChannel, phase: float):
    """Set the ``phase`` of a pulse ``channel``."""
    raise NotImplementedError()


def shift_phase(channel: PulseChannel, phase: float):
    """Shift the ``phase`` of a pulse ``channel``."""
    append_instruction(ShiftPhase(phase, channel))


def snapshot(label: str, snapshot_type: str = 'statevector'):
    """Simulator snapshot."""
    append_instruction(Snapshot(label, snapshot_type=snapshot_type))


def call_schedule(schedule: Schedule):
    """Call a pulse ``schedule`` in the builder context."""
    active_builder().call_schedule(schedule)


def call_circuit(circuit: QuantumCircuit, lazy=True):
    """Call a quantum ``circuit`` in the builder context."""
    active_builder().call_circuit(circuit, lazy=True)


def call(target: Union[QuantumCircuit, Schedule]):
    """Call the ``target`` within this builder context."""
    if isinstance(target, QuantumCircuit):
        call_circuit(target)
    elif isinstance(target, Schedule):
        call_schedule(target)
    raise PulseError(
        'Target of type "{}" is not supported.'.format(type(target)))


# Macros #######################################################################
def measure(qubit: int):
    backend = active_backend()
    call_schedule(macros.measure(qubits=[qubit],
                  inst_map=backend.defaults().instruction_schedule_map,
                  meas_map=backend.get().configuration().meas_map))


def delay_qubit(qubit: int, duration: int):
    with parallel(), group():
        for channel in qubit_channels(qubit):
            delay(channel, duration)


# Gate instructions ############################################################
def call_gate(gate, qubits):
    """Lower a circuit gate to pulse instruction."""
    try:
        iter(qubits)
    except TypeError:
        qubits = (qubits,)

    qc = QuantumCircuit(len(active_backend().configuration().n_qubits))
    qc.append(gate, qargs=qubits)
    call_circuit(qc)


def cx(control: int, target: int):
    call_gate(CnotGate(), control, target)


def u1(qubit: int, theta):
    call_gate(U1Gate(theta), qubit)


def u2(qubit: int, phi, lam):
    call_gate(U2Gate(phi, lam), qubit)


def u3(qubit: int, theta, phi, lam):
    call_gate(U3Gate(phi, lam), qubit)


def x(qubit: int):
    call_gate(XGate(), qubit)
