#!/usr/bin/env python3
"""Модель процессора 5-стадийного конвейерного CISC (IF -> ID -> OF -> EX -> MEM/WB).
Позволяет выполнять машинный код, сгенерированный на основе isa.py.

Модель включает в себя три основных компонента:
- `DataPath` -- память, регистровый файл, ALU, конвейерные регистры (защелки).
- `ControlUnit` -- декодирование, Forwarding Unit, HDU (Hazard Detection Unit), FSM.
- набор вспомогательных функций: `simulation`, `main`.
"""

import logging
import sys

# Импорт структур из предоставленного isa.py
from isa import (
    Opcode, InstrFormat, AddrMode,
    from_bytes, has_value
)

# "Пузырёк" для пустых стадий конвейера
BUBBLE = {"valid": False, "instr": None, "ctrl": {}}


class DataPath:
    """Тракт данных (пассивный).
    Хранит состояние всех элементов: памяти, регистров, флагов (SR)
    и 4-х конвейерных защелок (IF/ID, ID/OF, OF/EX, EX/MEMWB).
    """

    def __init__(self, data_memory_size, input_buffer):
        assert data_memory_size > 0, "Data_memory size should be non-zero"
        self.data_memory_size = data_memory_size
        self.d_mem = [0] * data_memory_size
        
        # Регистровый файл (R0-R15). R15 = SP
        self.rf = [0] * 16
        self.rf[15] = data_memory_size - 1  # Вершина стека

        self.pc = 0
        self.sr = {"Z": 0, "N": 0, "IE": 0}

        self.input_buffer = input_buffer
        self.output_buffer = []

        # Конвейерные регистры
        self.if_id = BUBBLE.copy()
        self.id_of = BUBBLE.copy()
        self.of_ex = BUBBLE.copy()
        self.ex_memwb = BUBBLE.copy()

    def zero(self):
        """Флаг Z (для интерфейса, если нужно напрямую)"""
        return self.sr["Z"] == 1

    def negative(self):
        """Флаг N"""
        return self.sr["N"] == 1


class ControlUnit:
    """Блок управления процессора (Hardwired + FSM).
    Симулирует синхронное продвижение тактов и разрешение конфликтов.
    Оценка стадий идет в обратном порядке (WB -> EX -> OF -> ID -> IF), 
    что безопасно симулирует поведение D-триггеров (защелкивание по фронту).
    """

    def __init__(self, program, data_path):
        self.program = program  # Память команд (словарь адрес -> Instruction)
        self.dp = data_path
        self._tick = 0
        self.halted = False

    def current_tick(self):
        return self._tick

    def _decode_instruction(self, instr):
        """Декодирование инструкции в набор управляющих сигналов (ctrl пучок)."""
        ctrl = {
            "alu_op": instr.opcode,
            "reg_write": False, "mem_read": False, "mem_write": False,
            "io_read": False, "io_write": False, "is_branch": False,
            "rs1": None, "rs2": None, "rd": None,
            "imm_src": 0, "imm_dst": 0,
            "base_reg_src": None, "base_reg_dst": None
        }

        if instr.opcode == Opcode.HALT:
            return ctrl

        if instr.opcode in [Opcode.JMP, Opcode.BEQ, Opcode.BNE, Opcode.BLT, Opcode.BGT, Opcode.CALL]:
            ctrl["is_branch"] = True
            ctrl["offset"] = instr.offset
            return ctrl

        v_idx = 0
        
        # Декодирование операнда-приёмника (dst)
        if len(instr.operands) > 0:
            op_dst = instr.operands[0]
            if op_dst.mode == AddrMode.REG:
                ctrl["rd"] = op_dst.reg
                ctrl["rs1"] = op_dst.reg  # В CISC приёмник также может быть источником (ADD R0, R1 -> R0=R0+R1)
                if instr.opcode not in [Opcode.CMP, Opcode.OUT]:
                    ctrl["reg_write"] = True
            else:
                if instr.opcode != Opcode.CMP:
                    ctrl["mem_write"] = True
                ctrl["base_reg_dst"] = op_dst.reg
                ctrl["rs1"] = op_dst.reg
                if has_value(op_dst.mode):
                    ctrl["imm_dst"] = instr.ext_values[v_idx] if v_idx < len(instr.ext_values) else 0
                    v_idx += 1
                if instr.opcode in [Opcode.ADD, Opcode.SUB, Opcode.MUL]:
                    ctrl["mem_read"] = True  # RMW операция

        # Декодирование операнда-источника (src)
        if len(instr.operands) > 1:
            op_src = instr.operands[1]
            if op_src.mode == AddrMode.REG:
                ctrl["rs2"] = op_src.reg
            elif op_src.mode == AddrMode.IMMEDIATE:
                ctrl["imm_src"] = instr.ext_values[v_idx] if v_idx < len(instr.ext_values) else 0
                v_idx += 1
            else:
                ctrl["mem_read"] = True
                ctrl["base_reg_src"] = op_src.reg
                ctrl["rs2"] = op_src.reg
                if has_value(op_src.mode):
                    ctrl["imm_src"] = instr.ext_values[v_idx] if v_idx < len(instr.ext_values) else 0
                    v_idx += 1

        # Переопределение для ввода/вывода
        if instr.opcode == Opcode.IN:
            ctrl["io_read"] = True
            ctrl["reg_write"] = True
            ctrl["mem_write"] = False
        elif instr.opcode == Opcode.OUT:
            ctrl["io_write"] = True
            ctrl["reg_write"] = False
            ctrl["mem_write"] = False

        return ctrl

    def _get_forwarded_value(self, reg_addr, original_val):
        """Forwarding Unit (Блок 7.4). Проброс данных из EX/MEMWB для избежания stall."""
        if reg_addr is None:
            return original_val
        
        # Проброс результата предыдущей инструкции (из регистра EX/MEMWB)
        if self.dp.ex_memwb["valid"] and self.dp.ex_memwb["ctrl"].get("reg_write"):
            if self.dp.ex_memwb["ctrl"].get("rd") == reg_addr:
                return self.dp.ex_memwb["alu_res"]
        
        # Дистанция 2 (MEM/WB) обрабатывается аппаратно за счет того, 
        # что запись WB выполняется до чтения ID в рамках одного такта.
        return original_val

    def process_next_tick(self):
        """Выполнение одного такта (реверсивная оценка стадий)."""
        if self.halted:
            raise StopIteration()

        # ---------------------------------------------------------
        # 5. СТАДИЯ MEM/WB (Выполняет запись первой)
        # ---------------------------------------------------------
        ex_latch = self.dp.ex_memwb
        if ex_latch["valid"]:
            ctrl = ex_latch["ctrl"]
            opcode = ctrl["alu_op"]
            
            # Разрешение записи в D-Mem
            if ctrl.get("mem_write") and not ctrl.get("io_write"):
                addr = ex_latch["eff_addr"]
                self.dp.d_mem[addr] = ex_latch["alu_res"]
            
            # Разрешение вывода в порт
            if ctrl.get("io_write"):
                char = chr(ex_latch["alu_res"] & 0xFF)
                logging.debug("OUT: %s", repr(char))
                self.dp.output_buffer.append(char)
                
            # Разрешение записи в RF
            if ctrl.get("reg_write"):
                rd = ctrl["rd"]
                data = ex_latch["alu_res"]
                
                if ctrl.get("io_read"):
                    if len(self.dp.input_buffer) == 0:
                        raise EOFError()
                    data = ord(self.dp.input_buffer.pop(0))
                    logging.debug("IN: %s", repr(chr(data)))
                elif ctrl.get("mem_read"):
                    data = self.dp.d_mem[ex_latch["eff_addr"]]
                    
                self.dp.rf[rd] = data

        # ---------------------------------------------------------
        # 4. СТАДИЯ EX (ALU)
        # ---------------------------------------------------------
        of_latch = self.dp.of_ex
        next_ex_memwb = BUBBLE.copy()
        
        if of_latch["valid"]:
            ctrl = of_latch["ctrl"]
            opcode = ctrl["alu_op"]
            
            # Форвардинг операндов
            alu_a = self._get_forwarded_value(ctrl.get("rs1"), of_latch["val1"])
            alu_b = self._get_forwarded_value(ctrl.get("rs2"), of_latch["val2"])
            
            # Если 2-й операнд immediate - берем его
            if ctrl.get("imm_src") != 0 or opcode in [Opcode.IN, Opcode.OUT]:
                alu_b = ctrl["imm_src"]

            # Если чтение из памяти - заменяем alu_b (или alu_a для RMW)
            if ctrl.get("mem_read") and opcode not in [Opcode.IN, Opcode.OUT]:
                mem_data = self.dp.d_mem[of_latch["eff_addr"]]
                if ctrl.get("mem_write"): # RMW
                    alu_a = mem_data
                else:
                    alu_b = mem_data
            
            # ALU Execute
            res = 0
            z, n = self.dp.sr["Z"], self.dp.sr["N"]
            
            if opcode == Opcode.ADD:
                res = alu_a + alu_b
            elif opcode == Opcode.SUB or opcode == Opcode.CMP:
                res = alu_a - alu_b
            elif opcode == Opcode.MUL:
                res = alu_a * alu_b
            elif opcode in [Opcode.MOV, Opcode.OUT]:
                res = alu_b  # PASS B
            else:
                res = alu_b # Default
                
            # Флаги
            if opcode in [Opcode.SUB, Opcode.CMP, Opcode.ADD]:
                z = 1 if res == 0 else 0
                n = 1 if res < 0 else 0
                self.dp.sr["Z"], self.dp.sr["N"] = z, n
                
            next_ex_memwb = {
                "valid": True,
                "instr": of_latch["instr"],
                "ctrl": ctrl,
                "alu_res": res,
                "eff_addr": of_latch["eff_addr"]
            }

        # ---------------------------------------------------------
        # 3. СТАДИЯ OF (Генерация адреса и ветвления)
        # ---------------------------------------------------------
        id_latch = self.dp.id_of
        next_of_ex = BUBBLE.copy()
        branch_taken = False
        branch_target = 0
        
        if id_latch["valid"]:
            ctrl = id_latch["ctrl"]
            instr = id_latch["instr"]
            
            # Форвардинг для базы адреса
            base_val = 0
            offset = 0
            if ctrl.get("base_reg_dst") is not None:
                base_val = self._get_forwarded_value(ctrl["base_reg_dst"], id_latch["val1"])
                offset = ctrl.get("imm_dst", 0)
            elif ctrl.get("base_reg_src") is not None:
                base_val = self._get_forwarded_value(ctrl["base_reg_src"], id_latch["val2"])
                offset = ctrl.get("imm_src", 0)
                
            eff_addr = base_val + offset
            
            # Разрешение ветвлений в OF (Penalty = 2)
            if ctrl.get("is_branch"):
                opcode = ctrl["alu_op"]
                target = instr.address + 1 + ctrl["offset"] # PC_next + offset
                z, n = self.dp.sr["Z"], self.dp.sr["N"]
                
                taken = False
                if opcode == Opcode.JMP: taken = True
                elif opcode == Opcode.BEQ: taken = (z == 1)
                elif opcode == Opcode.BNE: taken = (z == 0)
                elif opcode == Opcode.BLT: taken = (n == 1)
                elif opcode == Opcode.BGT: taken = (z == 0 and n == 0)
                
                if taken:
                    branch_taken = True
                    branch_target = target
                    logging.debug("Branch taken to %d (Penalty: 2)", target)

            next_of_ex = {
                "valid": not branch_taken, # Если прыгнули, OF/EX уходит пустым
                "instr": instr,
                "ctrl": ctrl,
                "val1": id_latch["val1"],
                "val2": id_latch["val2"],
                "eff_addr": eff_addr
            }

        # ---------------------------------------------------------
        # 2. СТАДИЯ ID (Декодирование и HDU)
        # ---------------------------------------------------------
        if_latch = self.dp.if_id
        next_id_of = BUBBLE.copy()
        hdu_stall = False
        
        if if_latch["valid"] and not branch_taken:
            instr = if_latch["instr"]
            if instr.opcode == Opcode.HALT:
                self.halted = True
                
            ctrl = self._decode_instruction(instr)
            
            # Чтение регистров
            val1 = self.dp.rf[ctrl["rs1"]] if ctrl["rs1"] is not None else 0
            val2 = self.dp.rf[ctrl["rs2"]] if ctrl["rs2"] is not None else 0

            # HDU: Консервативный stall (Конфликт RAW по памяти)
            # Если предыдущая инструкция пишет в память, а текущая читает - stall
            if ctrl.get("mem_read"):
                if id_latch["valid"] and id_latch["ctrl"].get("mem_write"):
                    hdu_stall = True
                if of_latch["valid"] and of_latch["ctrl"].get("mem_write"):
                    hdu_stall = True

            if not hdu_stall:
                next_id_of = {
                    "valid": True,
                    "instr": instr,
                    "ctrl": ctrl,
                    "val1": val1,
                    "val2": val2
                }
            else:
                logging.debug("HDU Stall (Memory Dependency)")

        # ---------------------------------------------------------
        # 1. СТАДИЯ IF (Выборка команды)
        # ---------------------------------------------------------
        next_if_id = BUBBLE.copy()
        
        if branch_taken:
            self.dp.pc = branch_target
            # Сброс IF/ID
        elif not hdu_stall:
            if self.dp.pc in self.program:
                instr = self.program[self.dp.pc]
                next_if_id = {"valid": True, "instr": instr}
                # В нашей симуляции pc шагает по логическим адресам
                self.dp.pc += 1 
        else:
            # При stall удерживаем PC и текущий IF/ID
            next_if_id = if_latch.copy()

        # ---------------------------------------------------------
        # Применение состояния (Клок)
        # ---------------------------------------------------------
        self.dp.ex_memwb = next_ex_memwb
        self.dp.of_ex = next_of_ex
        self.dp.id_of = next_id_of
        self.dp.if_id = next_if_id
        
        self._tick += 1

    def __repr__(self):
        """Строковое представление состояния конвейера."""
        def fmt(latch):
            if not latch["valid"]: return "---"
            return Opcode(latch["instr"].opcode).name
            
        return "TICK: {:4} PC: {:3} IF: {:4} | ID: {:4} | OF: {:4} | EX: {:4} | R0: {}".format(
            self._tick, self.dp.pc,
            fmt(self.dp.if_id), fmt(self.dp.id_of),
            fmt(self.dp.of_ex), fmt(self.dp.ex_memwb),
            self.dp.rf[0]
        )


def simulation(code, input_tokens, data_memory_size, limit):
    """Подготовка модели и запуск симуляции процессора."""
    
    # Преобразуем список инструкций в словарь по логическому адресу
    program = {instr.address: instr for instr in code}
    
    data_path = DataPath(data_memory_size, input_tokens)
    control_unit = ControlUnit(program, data_path)

    logging.debug("%s", control_unit)
    try:
        while control_unit.current_tick() < limit:
            control_unit.process_next_tick()
            logging.debug("%s", control_unit)
            
            # Если процессор Halted и конвейер пуст - выход
            if control_unit.halted and \
               not data_path.if_id["valid"] and not data_path.id_of["valid"] and \
               not data_path.of_ex["valid"] and not data_path.ex_memwb["valid"]:
                break
                
    except EOFError:
        logging.warning("Input buffer is empty!")
    except StopIteration:
        pass

    if control_unit.current_tick() >= limit:
        logging.warning("Limit exceeded!")
        
    return "".join(data_path.output_buffer), control_unit.current_tick()


def main(code_file, input_file):
    """Функция запуска модели процессора."""
    
    # Загрузка и парсинг бинарного кода
    with open(code_file, "rb") as file:
        binary_code = file.read()
    
    # Использование `from_bytes` из isa.py
    code = from_bytes(binary_code)

    # Загрузка ввода
    with open(input_file, encoding="utf-8") as file:
        input_text = file.read()
        input_token = list(input_text)

    output, ticks = simulation(
        code,
        input_tokens=input_token,
        data_memory_size=100,
        limit=2000,
    )

    print("output_buffer:", output)
    print("ticks:", ticks)


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.DEBUG)
    if len(sys.argv) == 3:
        _, code_file, input_file = sys.argv
        main(code_file, input_file)
    else:
        print("Wrong arguments: machine.py <code_file> <input_file>")