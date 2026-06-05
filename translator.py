#!/usr/bin/python3
"""Translator from the Lisp-like language described in README to ISA bytes."""

import argparse

from isa import AddrMode, Descriptor, Instruction, Opcode, Register, encode_instruction, has_value, to_bytes


STATIC_BASE = 0x100
STACK_TOP = 0x1000
PORT_CONSOLE = 0
ARITHMETIC_OPCODES = {"+": Opcode.ADD, "-": Opcode.SUB, "*": Opcode.MUL, "/": Opcode.DIV, "mod": Opcode.MOD}
COMPARE_OPCODES = {"<": Opcode.SETL, ">": Opcode.SETG, "=": Opcode.SETE}
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


class TranslationError(Exception):
    """Raised when source code cannot be translated."""


def node(kind, *values):
    return (kind,) + values


def is_node(value, kind):
    return isinstance(value, tuple) and len(value) > 0 and value[0] == kind


class CodeEmitter:
    def __init__(self):
        self.items = []
        self.label_counter = 0

    def new_label(self, prefix):
        self.label_counter += 1
        return "__{}_{}".format(prefix, self.label_counter)

    def label(self, name):
        self.items.append(node("label", name))

    def emit(self, opcode, operands=None, regs=None, func=None, target=None):
        instr = Instruction()
        instr.opcode = opcode
        instr.regs = list(regs or [])
        instr.operands = []
        instr.ext_values = []

        if operands is not None:
            operands = normalize_short_operands(operands)
            for operand in operands:
                mode, register, value, short = operand
                desc = Descriptor()
                desc.mode = mode
                desc.reg = register
                desc.short = short
                instr.operands.append(desc)
                if has_value(desc.mode):
                    instr.ext_values.append(value)

        if func is not None:
            instr.func_addr = node("function_ref", func)
        if target is not None:
            instr.offset = node("label_ref", target)

        self.items.append(instr)
        return instr

    def emit_mov(self, dst, src):
        return self.emit(Opcode.MOV, [dst, src])


def is_short_value(value):
    return -128 <= value <= 127


def normalize_short_operands(operands):
    """C2/C3 have one imm8 field, so keep at most one short value operand."""
    result = []
    short_used = False
    for operand in operands:
        mode, register, value, short = operand
        if short:
            if short_used:
                short = False
            else:
                short_used = True
        result.append(operand(mode, register, value, short))
    return result


def operand(mode, register=0, value=None, short=False):
    return (mode, register, value, short)


def reg(num):
    return operand(AddrMode.REG, num)


def imm(value, prefer_short=True):
    return operand(AddrMode.IMMEDIATE, value=value, short=prefer_short and is_short_value(value))


def absolute(address, prefer_short=False):
    return operand(AddrMode.ABSOLUTE, value=address, short=prefer_short and is_short_value(address))


def base_offset(register, offset, prefer_short=True):
    return operand(AddrMode.BASE_OFFSET, register, offset, prefer_short and is_short_value(offset))


def post_inc(register):
    return operand(AddrMode.POST_INC, register)


def tokenize(source):
    tokens = []
    i = 0

    while i < len(source):
        ch = source[i]

        if ch.isspace():
            i += 1
            continue
        if ch == ";":
            while i < len(source) and source[i] != "\n":
                i += 1
            continue
        if ch in "()":
            tokens.append(ch)
            i += 1
            continue
        if ch == '"':
            i += 1
            chars = []
            while i < len(source) and source[i] != '"':
                ch = source[i]
                if ch == "\\":
                    if i + 1 >= len(source):
                        raise TranslationError("unfinished escape sequence")
                    chars.append(ESCAPES.get(source[i + 1], source[i + 1]))
                    i += 2
                    continue
                if ch == "\n":
                    raise TranslationError("newline in string literal")
                chars.append(ch)
                i += 1
            if i >= len(source):
                raise TranslationError("unterminated string literal")
            i += 1
            tokens.append(node("string", "".join(chars)))
            continue

        start = i
        while i < len(source) and source[i] not in " \t\r\n();":
            i += 1
        tokens.append(source[start:i])

    return tokens


def parse(source):
    tokens = tokenize(source)
    pos = 0
    forms = []

    def parse_one():
        nonlocal pos
        if pos >= len(tokens):
            raise TranslationError("unexpected end of input")

        token = tokens[pos]
        pos += 1

        if token == "(":
            values = []
            while pos < len(tokens) and tokens[pos] != ")":
                values.append(parse_one())
            if pos >= len(tokens):
                raise TranslationError("expected ')'")
            pos += 1
            return values

        if token == ")":
            raise TranslationError("unexpected ')'")

        if is_node(token, "string"):
            return token

        try:
            return int(token)
        except ValueError:
            return token

    while pos < len(tokens):
        forms.append(parse_one())

    return forms


def symbol_name(value):
    if not isinstance(value, str):
        raise TranslationError("expected identifier")
    return value


def form_head(form):
    if isinstance(form, list) and form and isinstance(form[0], str):
        return form[0]
    return None


class Translator:
    def __init__(self, forms):
        self.forms = forms
        self.emitter = CodeEmitter()
        self.functions = {}
        self.globals = {}
        self.global_kinds = {}
        self.interrupts = {}
        self.string_pool = {}
        self.next_static_addr = STATIC_BASE
        self.need_print_str = False

    def translate(self):
        self.collect_declarations()
        self.emit_main()
        self.emit_functions()
        self.emit_interrupts()
        if self.need_print_str:
            self.emit_print_str()
        return self.resolve()

    def collect_declarations(self):
        for form in self.forms:
            self.collect_strings(form)
            head_name = form_head(form)
            if head_name == "defun":
                self.collect_function(form)
            elif head_name == "defvar":
                self.collect_global(form)
            elif head_name == "definterrupt":
                self.collect_interrupt(form)

    def collect_strings(self, form):
        if is_node(form, "string"):
            self.intern_string(form[1])
            return
        if isinstance(form, list):
            for item in form:
                self.collect_strings(item)

    def collect_function(self, form):
        if len(form) < 4:
            raise TranslationError("defun requires name, argument list and body")
        name = symbol_name(form[1])
        if name in self.functions:
            raise TranslationError("function '{}' is already defined".format(name))
        args_form = form[2]
        if not isinstance(args_form, list):
            raise TranslationError("defun '{}' argument list must be a list".format(name))
        args = [symbol_name(arg) for arg in args_form]
        if len(args) != len(set(args)):
            raise TranslationError("defun '{}' has duplicate arguments".format(name))
        self.functions[name] = {"args": args, "body": form[3:]}

    def collect_global(self, form):
        if len(form) != 3:
            raise TranslationError("defvar requires name and initializer")
        name = symbol_name(form[1])
        if name in self.globals:
            raise TranslationError("global '{}' is already defined".format(name))
        self.globals[name] = self.alloc_static(1)
        self.global_kinds[name] = self.expr_kind(form[2], use_globals=False)

    def collect_interrupt(self, form):
        if len(form) < 3:
            raise TranslationError("definterrupt requires vector and body")
        vector = form[1]
        if not isinstance(vector, int) or vector < 0 or vector > 255:
            raise TranslationError("interrupt vector must be an integer in range 0..255")
        if vector in self.interrupts:
            raise TranslationError("interrupt vector {} is already defined".format(vector))
        self.interrupts[vector] = {"label": "__interrupt_{}".format(vector), "body": form[2:]}

    def alloc_static(self, size):
        address = self.next_static_addr
        self.next_static_addr += size
        return address

    def intern_string(self, value):
        if value not in self.string_pool:
            self.string_pool[value] = self.alloc_static(len(value) + 1)
        return self.string_pool[value]

    def emit_main(self):
        self.emitter.label("__start")
        self.emitter.emit_mov(reg(Register.SP), imm(STACK_TOP, prefer_short=False))
        self.emitter.emit_mov(reg(Register.FP), reg(Register.SP))

        for value in self.string_pool:
            self.emit_string_initializer(value)

        for vector, info in self.interrupts.items():
            self.emitter.emit_mov(absolute(vector), imm(0, prefer_short=False))
            info["patch_addr_instruction"] = self.emitter.items[-1]

        for form in self.forms:
            head_name = form_head(form)
            if head_name == "defvar":
                name = symbol_name(form[1])
                self.compile_expr(form[2], {})
                self.emitter.emit_mov(absolute(self.globals[name]), reg(Register.R0))
                continue
            if head_name in ("defun", "definterrupt"):
                continue
            self.compile_expr(form, {})

        for vector, info in self.interrupts.items():
            instr = info["patch_addr_instruction"]
            instr.ext_values[-1] = node("label_ref", info["label"])

        self.emitter.emit(Opcode.HALT)

    def emit_string_initializer(self, value):
        address = self.intern_string(value)
        self.emitter.emit_mov(absolute(address), imm(len(value)))
        for index, ch in enumerate(value):
            self.emitter.emit_mov(absolute(address + index + 1), imm(ord(ch)))

    def emit_functions(self):
        for name, info in self.functions.items():
            self.emitter.label("__func_{}".format(name))
            self.emit_function_prologue()
            locals_map = self.make_param_map(info["args"])
            self.compile_expr_list(info["body"], locals_map)
            self.emit_function_epilogue(Opcode.RET)

    def emit_interrupts(self):
        for vector, info in self.interrupts.items():
            self.emitter.label(info["label"])
            self.emitter.emit(Opcode.PUSHM, regs=[Register.R0, Register.R1, Register.R2, Register.R3, Register.R4, Register.R5])
            self.compile_expr_list(info["body"], {})
            self.emitter.emit(Opcode.POPM, regs=[Register.R5, Register.R4, Register.R3, Register.R2, Register.R1, Register.R0])
            self.emitter.emit(Opcode.IRET)

    def emit_function_prologue(self):
        self.emitter.emit(Opcode.PUSHM, regs=[Register.FP])
        self.emitter.emit_mov(reg(Register.FP), reg(Register.SP))

    def emit_function_epilogue(self, opcode):
        self.emitter.emit_mov(reg(Register.SP), reg(Register.FP))
        self.emitter.emit(Opcode.POPM, regs=[Register.FP])
        self.emitter.emit(opcode)

    def make_param_map(self, args):
        locals_map = {}
        for index, name in enumerate(args):
            locals_map[name] = base_offset(Register.FP, 2 + index)
        return locals_map

    def compile_expr_list(self, exprs, locals_map):
        if len(exprs) == 0:
            self.emitter.emit_mov(reg(Register.R0), imm(0))
            return
        for expr in exprs:
            self.compile_expr(expr, locals_map)

    def compile_expr(self, expr, locals_map):
        if isinstance(expr, int):
            self.emitter.emit_mov(reg(Register.R0), imm(expr))
            return

        if is_node(expr, "string"):
            address = self.intern_string(expr[1])
            self.emitter.emit_mov(reg(Register.R0), imm(address, prefer_short=False))
            return

        if isinstance(expr, str):
            self.emitter.emit_mov(reg(Register.R0), self.lookup_variable(expr, locals_map))
            return

        name = form_head(expr)
        if name is None:
            raise TranslationError("list head must be an identifier")
        args = expr[1:]

        if name == "if":
            self.compile_if(args, locals_map)
        elif name == "setq":
            self.compile_setq(args, locals_map)
        elif name == "loop":
            self.compile_loop(args, locals_map)
        elif name == "print":
            self.compile_print(args, locals_map)
        elif name == "progn":
            self.compile_expr_list(args, locals_map)
        elif name in ("+", "-", "*", "/", "mod", "=", "<", ">"):
            self.compile_binary(name, args, locals_map)
        elif name == "in":
            self.compile_in(args, locals_map)
        elif name == "out":
            self.compile_out(args, locals_map)
        elif name in ("defvar", "defun", "definterrupt"):
            self.emitter.emit_mov(reg(Register.R0), imm(0))
        else:
            self.compile_call(name, args, locals_map)

    def lookup_variable(self, name, locals_map):
        if name in locals_map:
            return locals_map[name]
        if name in self.globals:
            return absolute(self.globals[name])
        raise TranslationError("unknown variable '{}'".format(name))

    def compile_setq(self, args, locals_map):
        if len(args) != 2:
            raise TranslationError("setq requires variable and expression")
        name = symbol_name(args[0])
        dst = self.lookup_variable(name, locals_map)
        self.compile_expr(args[1], locals_map)
        self.emitter.emit_mov(dst, reg(Register.R0))
        if name in self.globals and name not in locals_map:
            self.global_kinds[name] = self.expr_kind(args[1])

    def compile_if(self, args, locals_map):
        if len(args) != 3:
            raise TranslationError("if requires condition, then-expression and else-expression")
        else_label = self.emitter.new_label("else")
        end_label = self.emitter.new_label("endif")

        self.compile_expr(args[0], locals_map)
        self.emitter.emit(Opcode.CMP, [reg(Register.R0), imm(0)])
        self.emitter.emit(Opcode.BEQ, target=else_label)
        self.compile_expr(args[1], locals_map)
        self.emitter.emit(Opcode.JMP, target=end_label)
        self.emitter.label(else_label)
        self.compile_expr(args[2], locals_map)
        self.emitter.label(end_label)

    def compile_loop(self, args, locals_map):
        if len(args) < 2:
            raise TranslationError("loop requires condition and at least one body expression")
        start_label = self.emitter.new_label("loop")
        end_label = self.emitter.new_label("endloop")

        self.emitter.emit_mov(reg(Register.R0), imm(0))
        self.emitter.emit(Opcode.PUSHM, regs=[Register.R0])
        self.emitter.label(start_label)
        self.compile_expr(args[0], locals_map)
        self.emitter.emit(Opcode.CMP, [reg(Register.R0), imm(0)])
        self.emitter.emit(Opcode.BEQ, target=end_label)
        self.compile_expr_list(args[1:], locals_map)
        self.emitter.emit_mov(base_offset(Register.SP, 0), reg(Register.R0))
        self.emitter.emit(Opcode.JMP, target=start_label)
        self.emitter.label(end_label)
        self.emitter.emit(Opcode.POPM, regs=[Register.R0])

    def compile_print(self, args, locals_map):
        if len(args) != 1:
            raise TranslationError("print requires one expression")
        self.compile_expr(args[0], locals_map)
        if self.expr_kind(args[0]) == "string":
            self.need_print_str = True
            self.emitter.emit(Opcode.CALL, target="__print_str")
        else:
            self.emitter.emit(Opcode.OUT, [reg(Register.R0), imm(PORT_CONSOLE)])

    def compile_binary(self, operator, args, locals_map):
        if len(args) != 2:
            raise TranslationError("'{}' requires two operands".format(operator))
        self.compile_expr(args[0], locals_map)
        self.emitter.emit(Opcode.PUSHM, regs=[Register.R0])
        self.compile_expr(args[1], locals_map)
        self.emitter.emit(Opcode.POPM, regs=[Register.R1])

        if operator in ARITHMETIC_OPCODES:
            self.emitter.emit(ARITHMETIC_OPCODES[operator], [reg(Register.R1), reg(Register.R0)])
            self.emitter.emit_mov(reg(Register.R0), reg(Register.R1))
        else:
            self.emitter.emit(Opcode.CMP, [reg(Register.R1), reg(Register.R0)])
            self.emitter.emit(COMPARE_OPCODES[operator], [reg(Register.R0)])

    def compile_call(self, name, args, locals_map):
        if name not in self.functions:
            raise TranslationError("unknown function '{}'".format(name))
        expected = len(self.functions[name]["args"])
        if len(args) != expected:
            raise TranslationError("function '{}' expects {} argument(s), got {}".format(name, expected, len(args)))
        if len(args) > 5:
            raise TranslationError("function calls with more than 5 arguments are not supported by this translator")

        for arg in args:
            self.compile_expr(arg, locals_map)
            self.emitter.emit(Opcode.PUSHM, regs=[Register.R0])

        arg_regs = [Register.R1 + i for i in range(len(args))]
        for register in reversed(arg_regs):
            self.emitter.emit(Opcode.POPM, regs=[register])

        if len(args) == 0:
            self.emitter.emit(Opcode.CALL, target="__func_{}".format(name))
        else:
            self.emitter.emit(Opcode.INVOKE, regs=arg_regs, func="__func_{}".format(name))
            self.emitter.emit(Opcode.ADD, [reg(Register.SP), imm(len(args))])

    def compile_in(self, args, locals_map):
        if len(args) != 1:
            raise TranslationError("in requires one port expression")
        if isinstance(args[0], int):
            self.emitter.emit(Opcode.IN, [imm(args[0]), reg(Register.R0)])
            return
        self.compile_expr(args[0], locals_map)
        self.emitter.emit_mov(reg(Register.R1), reg(Register.R0))
        self.emitter.emit(Opcode.IN, [reg(Register.R1), reg(Register.R0)])

    def compile_out(self, args, locals_map):
        if len(args) != 2:
            raise TranslationError("out requires value and port expressions")
        self.compile_expr(args[0], locals_map)
        self.emitter.emit(Opcode.PUSHM, regs=[Register.R0])
        self.compile_expr(args[1], locals_map)
        self.emitter.emit_mov(reg(Register.R1), reg(Register.R0))
        self.emitter.emit(Opcode.POPM, regs=[Register.R0])
        self.emitter.emit(Opcode.OUT, [reg(Register.R0), reg(Register.R1)])

    def expr_kind(self, expr, use_globals=True):
        if is_node(expr, "string"):
            return "string"
        if isinstance(expr, str):
            if use_globals:
                return self.global_kinds.get(expr, "integer")
            return "integer"
        head_name = form_head(expr)
        if head_name is not None:
            if head_name == "if" and len(expr) == 4:
                then_kind = self.expr_kind(expr[2], use_globals)
                else_kind = self.expr_kind(expr[3], use_globals)
                if then_kind == else_kind:
                    return then_kind
            if head_name == "progn" and len(expr) > 1:
                return self.expr_kind(expr[-1], use_globals)
            if head_name == "setq" and len(expr) == 3:
                return self.expr_kind(expr[2], use_globals)
        return "integer"

    def emit_print_str(self):
        loop_label = "__print_str_loop"
        end_label = "__print_str_end"

        self.emitter.label("__print_str")
        self.emitter.emit(Opcode.PUSHM, regs=[Register.R1, Register.R2, Register.R3])
        self.emitter.emit_mov(reg(Register.R3), reg(Register.R0))
        self.emitter.emit_mov(reg(Register.R1), reg(Register.R0))
        self.emitter.emit_mov(reg(Register.R2), post_inc(Register.R1))
        self.emitter.label(loop_label)
        self.emitter.emit(Opcode.CMP, [reg(Register.R2), imm(0)])
        self.emitter.emit(Opcode.BEQ, target=end_label)
        self.emitter.emit_mov(reg(Register.R0), post_inc(Register.R1))
        self.emitter.emit(Opcode.OUT, [reg(Register.R0), imm(PORT_CONSOLE)])
        self.emitter.emit(Opcode.SUB, [reg(Register.R2), imm(1)])
        self.emitter.emit(Opcode.JMP, target=loop_label)
        self.emitter.label(end_label)
        self.emitter.emit_mov(reg(Register.R0), reg(Register.R3))
        self.emitter.emit(Opcode.POPM, regs=[Register.R3, Register.R2, Register.R1])
        self.emitter.emit(Opcode.RET)

    def resolve(self):
        labels = {}
        address = 0
        instructions = []

        for item in self.emitter.items:
            if is_node(item, "label"):
                labels[item[1]] = address
                continue
            item.address = address
            instructions.append(item)
            address += len(encode_instruction_with_placeholders(item))

        for instr in instructions:
            if is_node(instr.offset, "label_ref"):
                label_name = instr.offset[1]
                if label_name not in labels:
                    raise TranslationError("unknown label '{}'".format(label_name))
                instr.offset = labels[label_name] - (instr.address + 1)
            if is_node(instr.func_addr, "function_ref"):
                label_name = instr.func_addr[1]
                if label_name not in labels:
                    raise TranslationError("unknown function label '{}'".format(label_name))
                instr.func_addr = labels[label_name]
            for index, value in enumerate(instr.ext_values):
                if is_node(value, "label_ref"):
                    label_name = value[1]
                    if label_name not in labels:
                        raise TranslationError("unknown label '{}'".format(label_name))
                    instr.ext_values[index] = labels[label_name]

        return instructions


def encode_instruction_with_placeholders(instr):
    saved_offset = instr.offset
    saved_func_addr = instr.func_addr
    saved_ext_values = list(instr.ext_values)
    if is_node(instr.offset, "label_ref"):
        instr.offset = 0
    if is_node(instr.func_addr, "function_ref"):
        instr.func_addr = 0
    instr.ext_values = [0 if is_node(value, "label_ref") else value for value in instr.ext_values]
    words = encode_instruction(instr)
    instr.offset = saved_offset
    instr.func_addr = saved_func_addr
    instr.ext_values = saved_ext_values
    return words


def translate_source(source):
    forms = parse(source)
    return Translator(forms).translate()


def main():
    parser = argparse.ArgumentParser(description="Translate Lisp-like source code to processor machine code.")
    parser.add_argument("source", help="input source file")
    parser.add_argument("target", help="output binary file")
    args = parser.parse_args()

    with open(args.source, encoding="utf-8") as file:
        source = file.read()

    instructions = translate_source(source)

    with open(args.target, "wb") as file:
        file.write(to_bytes(instructions))

    print("source LoC:", len(source.splitlines()))
    print("instructions:", len(instructions))
    print("machine words:", sum(len(encode_instruction(instr)) for instr in instructions))


if __name__ == "__main__":
    main()
