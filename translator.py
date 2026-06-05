#!/usr/bin/python3

import sys
from isa import AddrMode, Descriptor, Instruction, Opcode, Register as R, encode_instruction, has_value, to_bytes

# Таблицы соответствия операций
ARITH = {"+": Opcode.ADD, "-": Opcode.SUB, "*": Opcode.MUL, "/": Opcode.DIV, "mod": Opcode.MOD}
COMP = {"<": Opcode.SETL, ">": Opcode.SETG, "=": Opcode.SETE}

# Хэлперы для генерации операндов
def reg(n): return (AddrMode.REG, n, None, False)
def imm(v, short=True): return (AddrMode.IMMEDIATE, 0, v, short and -128 <= v <= 127)
def abs_addr(v): return (AddrMode.ABSOLUTE, 0, v, False)
def base_off(r, off): return (AddrMode.BASE_OFFSET, r, off, -128 <= off <= 127)
def p_inc(r): return (AddrMode.POST_INC, r, None, False)

def tokenize(s):
    """Простейший лексер. Разбивает исходник на токены, обрабатывает строки с экранированием."""
    t, i = [], 0
    while i < len(s):
        c = s[i]
        if c.isspace(): i += 1
        elif c == ';':  # Пропуск комментариев
            while i < len(s) and s[i] != '\n': i += 1
        elif c in '()':
            t.append(c); i += 1
        elif c == '"':  # Разбор строк
            i += 1; chars = []
            while s[i] != '"':
                if s[i] == '\\':
                    chars.append({'n':'\n','t':'\t','r':'\r','"':'"','\\':'\\'}.get(s[i+1], s[i+1]))
                    i += 2
                else: chars.append(s[i]); i += 1
            t.append(("string", "".join(chars))); i += 1
        else:           # Идентификаторы и числа
            start = i
            while i < len(s) and s[i] not in " \t\r\n();": i += 1
            t.append(s[start:i])
    return t

def parse(s):
    """Парсер токенов в рекурсивные списки (AST)."""
    tokens = tokenize(s)
    pos = [0]
    
    def p():
        t = tokens[pos[0]]; pos[0] += 1
        if t == '(':
            res = []
            while tokens[pos[0]] != ')': res.append(p())
            pos[0] += 1
            return res
        if isinstance(t, tuple): return t
        try: return int(t)
        except ValueError: return t
        
    res = []
    while pos[0] < len(tokens): res.append(p())
    return res

class Translator:
    """Объединенный класс транслятора и генератора кода."""
    def __init__(self, forms):
        self.forms = forms
        self.items, self.lbl_cnt = [], 0
        self.funcs, self.globals, self.g_kinds, self.ints = {}, {}, {}, {}
        self.strs, self.s_addr = {}, 0x100
        self.need_pstr = False

    def lbl(self, name): 
        self.items.append(("label", name))

    def new_lbl(self, pfx):
        self.lbl_cnt += 1
        return f"__{pfx}_{self.lbl_cnt}"

    def emit(self, op, ops=None, regs=None, func=None, tgt=None):
        """Создает и добавляет инструкцию."""
        i = Instruction()
        i.opcode, i.regs, i.operands, i.ext_values = op, list(regs or []), [], []
        if ops:
            short_used = False
            for m, r, v, s in ops:
                if s:  # Гарантируем лишь одно short-значение (требование ISA)
                    if short_used: s = False
                    else: short_used = True
                d = Descriptor()
                d.mode, d.reg, d.short = m, r, s
                i.operands.append(d)
                if has_value(m): i.ext_values.append(v)
                
        if func: i.func_addr = ("fref", func)
        if tgt: i.offset = ("lref", tgt)
        self.items.append(i)
        return i

    def trans(self):
        """Основной цикл трансляции."""
        # 1. Сбор деклараций
        def coll_strs(f):
            if isinstance(f, tuple):
                if f[1] not in self.strs:
                    self.strs[f[1]] = self.s_addr
                    self.s_addr += len(f[1]) + 1
            elif isinstance(f, list):
                for x in f: coll_strs(x)
        
        for f in self.forms:
            coll_strs(f)
            h = f[0] if isinstance(f, list) else None
            if h == "defun": self.funcs[f[1]] = {"args": f[2], "body": f[3:]}
            elif h == "defvar":
                self.globals[f[1]] = self.s_addr; self.s_addr += 1
                self.g_kinds[f[1]] = self.e_kind(f[2], False)
            elif h == "definterrupt": self.ints[f[1]] = {"lbl": f"__int_{f[1]}", "body": f[2:]}

        # 2. Инициализация (Точка входа __start)
        self.lbl("__start")
        self.emit(Opcode.MOV, [reg(R.SP), imm(0x1000, False)])
        self.emit(Opcode.MOV, [reg(R.FP), reg(R.SP)])

        # Запись строк в память
        for v, a in self.strs.items():
            self.emit(Opcode.MOV, [abs_addr(a), imm(len(v))])
            for idx, c in enumerate(v):
                self.emit(Opcode.MOV, [abs_addr(a + idx + 1), imm(ord(c))])

        # Резерв прерываний
        for v, info in self.ints.items():
            self.emit(Opcode.MOV, [abs_addr(v), imm(0, False)])
            info["patch"] = self.items[-1]

        # Выполнение выражений вне функций
        for f in self.forms:
            h = f[0] if isinstance(f, list) else None
            if h == "defvar":
                self.ce(f[2], {})
                self.emit(Opcode.MOV, [abs_addr(self.globals[f[1]]), reg(R.R0)])
            elif h not in ("defun", "definterrupt"): self.ce(f, {})

        # Прошивка прерываний актуальными адресами
        for v, info in self.ints.items():
            info["patch"].ext_values[-1] = ("lref", info["lbl"])

        self.emit(Opcode.HALT)

        # 3. Генерация кода пользовательских функций
        for n, info in self.funcs.items():
            self.lbl(f"__f_{n}")
            self.emit(Opcode.PUSHM, regs=[R.FP])
            self.emit(Opcode.MOV, [reg(R.FP), reg(R.SP)])
            # Кадр: [saved FP, return PC, argN, ..., arg1]. Аргументы кладутся на стек
            # слева направо, поэтому первый аргумент оказывается по наибольшему смещению,
            # а последний — по [FP+2]. Доступ: arg_i -> [FP + 2 + (nargs-1-i)].
            nargs = len(info["args"])
            self.ce_list(info["body"], {arg: base_off(R.FP, 2 + (nargs - 1 - i))
                                        for i, arg in enumerate(info["args"])})
            self.emit(Opcode.MOV, [reg(R.SP), reg(R.FP)])
            self.emit(Opcode.POPM, regs=[R.FP])
            self.emit(Opcode.RET)

        # 4. Генерация обработчиков прерываний
        for v, info in self.ints.items():
            self.lbl(info["lbl"])
            self.emit(Opcode.PUSHM, regs=[R.R0, R.R1, R.R2, R.R3, R.R4, R.R5])
            self.ce_list(info["body"], {})
            self.emit(Opcode.POPM, regs=[R.R5, R.R4, R.R3, R.R2, R.R1, R.R0])
            self.emit(Opcode.IRET)

        # 5. Генерация вспомогательной функции печати строк (если нужно)
        if self.need_pstr:
            self.lbl("__pstr")
            self.emit(Opcode.PUSHM, regs=[R.R1, R.R2, R.R3])
            self.emit(Opcode.MOV, [reg(R.R3), reg(R.R0)])
            self.emit(Opcode.MOV, [reg(R.R1), reg(R.R0)])
            self.emit(Opcode.MOV, [reg(R.R2), p_inc(R.R1)])
            self.lbl("__ps_l")
            self.emit(Opcode.CMP, [reg(R.R2), imm(0)])
            self.emit(Opcode.BEQ, tgt="__ps_e")
            self.emit(Opcode.MOV, [reg(R.R0), p_inc(R.R1)])
            self.emit(Opcode.OUT, [reg(R.R0), imm(0)]) # PORT_CONSOLE = 0
            self.emit(Opcode.SUB, [reg(R.R2), imm(1)])
            self.emit(Opcode.JMP, tgt="__ps_l")
            self.lbl("__ps_e")
            self.emit(Opcode.MOV, [reg(R.R0), reg(R.R3)])
            self.emit(Opcode.POPM, regs=[R.R3, R.R2, R.R1])
            self.emit(Opcode.RET)

        # 6. Разрешение меток в адреса (Resolve)
        labels, addr, instrs = {}, 0, []
        for i in self.items:
            if isinstance(i, tuple) and i[0] == "label": labels[i[1]] = addr
            else:
                i.address = addr; instrs.append(i)
                addr += len(self.enc(i))

        for i in instrs:
            if isinstance(i.offset, tuple): i.offset = labels[i.offset[1]] - (i.address + 1)
            if isinstance(i.func_addr, tuple): i.func_addr = labels[i.func_addr[1]]
            i.ext_values = [labels[v[1]] if isinstance(v, tuple) else v for v in i.ext_values]

        return instrs

    def enc(self, i):
        """Временное кодирование для расчета смещений меток."""
        so, sf, se = i.offset, i.func_addr, list(i.ext_values)
        if isinstance(so, tuple): i.offset = 0
        if isinstance(sf, tuple): i.func_addr = 0
        i.ext_values = [0 if isinstance(v, tuple) else v for v in se]
        w = encode_instruction(i)
        i.offset, i.func_addr, i.ext_values = so, sf, se
        return w

    def ce_list(self, exprs, lm):
        if not exprs: self.emit(Opcode.MOV, [reg(R.R0), imm(0)])
        for e in exprs: self.ce(e, lm)

    def lvar(self, n, lm): 
        return lm[n] if n in lm else abs_addr(self.globals[n])

    def ce(self, e, lm):
        """Компиляция одного выражения (compile_expr)."""
        if isinstance(e, int): self.emit(Opcode.MOV, [reg(R.R0), imm(e)])
        elif isinstance(e, tuple): self.emit(Opcode.MOV, [reg(R.R0), imm(self.strs[e[1]], False)])
        elif isinstance(e, str): self.emit(Opcode.MOV, [reg(R.R0), self.lvar(e, lm)])
        else:
            h, args = e[0], e[1:]
            if h == "if":
                el, en = self.new_lbl("el"), self.new_lbl("en")
                self.ce(args[0], lm)
                self.emit(Opcode.CMP, [reg(R.R0), imm(0)])
                self.emit(Opcode.BEQ, tgt=el)
                self.ce(args[1], lm)
                self.emit(Opcode.JMP, tgt=en)
                self.lbl(el); self.ce(args[2], lm); self.lbl(en)
            elif h == "setq":
                self.ce(args[1], lm)
                self.emit(Opcode.MOV, [self.lvar(args[0], lm), reg(R.R0)])
                if args[0] in self.globals and args[0] not in lm: self.g_kinds[args[0]] = self.e_kind(args[1])
            elif h == "loop":
                sl, en = self.new_lbl("lp"), self.new_lbl("le")
                self.emit(Opcode.MOV, [reg(R.R0), imm(0)])
                self.emit(Opcode.PUSHM, regs=[R.R0])
                self.lbl(sl); self.ce(args[0], lm)
                self.emit(Opcode.CMP, [reg(R.R0), imm(0)])
                self.emit(Opcode.BEQ, tgt=en)
                self.ce_list(args[1:], lm)
                self.emit(Opcode.MOV, [base_off(R.SP, 0), reg(R.R0)])
                self.emit(Opcode.JMP, tgt=sl)
                self.lbl(en); self.emit(Opcode.POPM, regs=[R.R0])
            elif h == "print":
                self.ce(args[0], lm)
                if self.e_kind(args[0]) == "string":
                    self.need_pstr = True
                    self.emit(Opcode.CALL, tgt="__pstr")
                else: self.emit(Opcode.OUT, [reg(R.R0), imm(0)])
            elif h == "progn": self.ce_list(args, lm)
            elif h in ARITH or h in COMP:
                self.ce(args[0], lm); self.emit(Opcode.PUSHM, regs=[R.R0])
                self.ce(args[1], lm); self.emit(Opcode.POPM, regs=[R.R1])
                if h in ARITH:
                    self.emit(ARITH[h], [reg(R.R1), reg(R.R0)])
                    self.emit(Opcode.MOV, [reg(R.R0), reg(R.R1)])
                else:
                    self.emit(Opcode.CMP, [reg(R.R1), reg(R.R0)])
                    self.emit(COMP[h], [reg(R.R0)])
            elif h == "in":
                if isinstance(args[0], int): self.emit(Opcode.IN, [imm(args[0]), reg(R.R0)])
                else:
                    self.ce(args[0], lm)
                    self.emit(Opcode.MOV, [reg(R.R1), reg(R.R0)])
                    self.emit(Opcode.IN, [reg(R.R1), reg(R.R0)])
            elif h == "out":
                self.ce(args[0], lm); self.emit(Opcode.PUSHM, regs=[R.R0])
                self.ce(args[1], lm); self.emit(Opcode.MOV, [reg(R.R1), reg(R.R0)])
                self.emit(Opcode.POPM, regs=[R.R0])
                self.emit(Opcode.OUT, [reg(R.R0), reg(R.R1)])
            elif h in ("defvar", "defun", "definterrupt"): self.emit(Opcode.MOV, [reg(R.R0), imm(0)])
            else: # Вызов пользовательской функции (README: передача аргументов
                  # через стек + CALL, без отдельной инструкции вызова).
                # Аргументы вычисляются слева направо и кладутся на стек.
                for a in args:
                    self.ce(a, lm); self.emit(Opcode.PUSHM, regs=[R.R0])
                self.emit(Opcode.CALL, tgt=f"__f_{h}")
                # Снятие аргументов со стека после возврата (результат уже в R0).
                if args: self.emit(Opcode.ADD, [reg(R.SP), imm(len(args))])

    def e_kind(self, e, ug=True):
        """Статический вывод типа выражения."""
        if isinstance(e, tuple): return "string"
        if isinstance(e, str): return self.g_kinds.get(e, "integer") if ug else "integer"
        if isinstance(e, list) and e:
            h = e[0]
            if h == "if" and len(e) == 4: return self.e_kind(e[2], ug) if self.e_kind(e[2], ug) == self.e_kind(e[3], ug) else "integer"
            if h == "progn" and len(e) > 1: return self.e_kind(e[-1], ug)
            if h == "setq" and len(e) == 3: return self.e_kind(e[2], ug)
        return "integer"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: translator.py <source.lisp> <target.bin>")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as file:
        source = file.read()

    instructions = Translator(parse(source)).trans()

    with open(sys.argv[2], "wb") as file:
        file.write(to_bytes(instructions))

    print("source LoC:", len(source.splitlines()))
    print("instructions:", len(instructions))
    print("machine words:", sum(len(encode_instruction(i)) for i in instructions))