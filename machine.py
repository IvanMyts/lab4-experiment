#!/usr/bin/python3
"""Минимальная модель процессора для текущего описания ISA.

Модель состоит из двух аппаратных блоков:

- `Datapath` -- хранит регистровое состояние, память данных и буферы ввода-вывода;
- `ControlUnit` -- выбирает инструкции из памяти команд и управляет тактами.

Сейчас реализован только счётчик команд `PC` с сумматором `+1`.
Остальные инструкции, кроме `HALT`, декодируются как валидные инструкции ISA,
но пока не исполняют своих семантических действий.
"""

import logging
import sys

from isa import Opcode, from_bytes


class Datapath:
    """Тракт данных процессора.

    На этом этапе в нём реализован только `program_counter` и сигнал его
    защёлкивания через сумматор `+1`. Поля памяти данных, регистров и
    ввода-вывода сохранены как часть состояния будущей полноценной симуляции.
    """

    def __init__(self, data_memory_size, input_buffer):
        assert data_memory_size > 0, "Data memory size should be non-zero"

        self.data_memory_size = data_memory_size
        self.data_memory = [0] * data_memory_size
        self.registers = [0] * 16
        self.status_register = {
            "Z": False,
            "N": False,
            "IE": False,
        }
        self.program_counter = 0
        self.input_buffer = input_buffer
        self.output_buffer = []

    def signal_latch_program_counter(self):
        """Защёлкнуть новое значение `PC`.

        Источник значения сейчас единственный: выход сумматора `PC + 1`.
        """
        self.program_counter = self.program_counter + 1


class ControlUnit:
    """Блок управления процессора.

    `ControlUnit` хранит память команд, выбирает инструкцию по `PC`, проверяет
    `HALT` и продвигает модельное время. Полная семантика инструкций будет
    добавляться поверх этого каркаса без изменения внешней функции `simulation`.
    """

    def __init__(self, program, datapath):
        self.program = program
        self.datapath = datapath
        self.data_path = datapath
        self._tick = 0
        self.step = 0

    @property
    def program_counter(self):
        """Текущее значение `PC` для совместимости со старой моделью."""
        return self.datapath.program_counter

    def signal_latch_program_counter(self):
        """Пробросить управляющий сигнал защёлкивания `PC` в `Datapath`."""
        self.datapath.signal_latch_program_counter()

    def tick(self):
        """Продвинуть модельное время процессора вперёд на один такт."""
        self._tick += 1

    def current_tick(self):
        """Текущее модельное время процессора в тактах."""
        return self._tick

    def current_instruction(self):
        """Вернуть инструкцию, на которую указывает `PC`."""
        pc = self.datapath.program_counter
        if pc >= len(self.program):
            raise StopIteration()
        return self.program[pc]

    def process_next_tick(self):
        """Выполнить один такт симуляции.

        Сейчас поддержаны:

        - выборка текущей инструкции;
        - остановка по `HALT`;
        - переход к следующей инструкции через `PC + 1`.
        """
        instr = self.current_instruction()

        if instr.opcode == Opcode.HALT:
            raise StopIteration()

        self.signal_latch_program_counter()
        self.tick()

    def __repr__(self):
        """Вернуть строковое представление состояния процессора."""
        pc = self.datapath.program_counter
        if pc < len(self.program):
            instr = self.program[pc]
            instr_repr = instruction_to_string(instr)
        else:
            instr_repr = "<out of program>"

        return "TICK: {:3} PC: {:3} {}".format(
            self._tick,
            pc,
            instr_repr,
        )


def instruction_to_string(instr):
    """Сформировать компактное текстовое представление инструкции ISA."""
    opcode_name = opcode_to_name(instr.opcode)

    parts = [opcode_name]
    if instr.operands:
        parts.append("operands={}".format(len(instr.operands)))
    if instr.ext_values:
        parts.append("ext={}".format(instr.ext_values))
    if instr.regs:
        parts.append("regs={}".format(instr.regs))
    if instr.offset != 0:
        parts.append("offset={}".format(instr.offset))
    if instr.func_addr != 0:
        parts.append("func_addr={}".format(instr.func_addr))

    return " ".join(parts)


def opcode_to_name(opcode):
    """Вернуть имя opcode из класса `Opcode`."""
    for name, value in Opcode.__dict__.items():
        if name.isupper() and value == opcode:
            return name
    return "UNKNOWN(0x{:02X})".format(opcode)


def simulation(code, input_tokens, data_memory_size, limit):
    """Подготовка модели и запуск симуляции процессора.

    Длительность моделирования ограничена:

    - количеством выполненных тактов (`limit`);
    - инструкцией `HALT`;
    - выходом `PC` за пределы памяти команд.
    """
    datapath = Datapath(data_memory_size, input_tokens)
    control_unit = ControlUnit(code, datapath)

    logging.debug("%s", control_unit)
    try:
        while control_unit.current_tick() < limit:
            control_unit.process_next_tick()
            logging.debug("%s", control_unit)
    except EOFError:
        logging.warning("Input buffer is empty!")
    except StopIteration:
        pass

    if control_unit.current_tick() >= limit:
        logging.warning("Limit exceeded!")
    logging.info("output_buffer: %s", repr("".join(datapath.output_buffer)))
    return "".join(datapath.output_buffer), control_unit.current_tick()


def main(code_file, input_file):
    """Запустить модель процессора по бинарному машинному коду."""
    with open(code_file, "rb") as file:
        binary_code = file.read()
    code = from_bytes(binary_code)

    with open(input_file, encoding="utf-8") as file:
        input_tokens = list(file.read())

    output, ticks = simulation(
        code,
        input_tokens=input_tokens,
        data_memory_size=100,
        limit=2000,
    )

    print(output)
    print("ticks:", ticks)


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.DEBUG)
    assert len(sys.argv) == 3, "Wrong arguments: machine.py <code_file> <input_file>"
    _, code_file, input_file = sys.argv
    main(code_file, input_file)
