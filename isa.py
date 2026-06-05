# ==========================================
# 1. КОНСТАНТЫ И СТРУКТУРЫ
# ==========================================

# Константы для регистров процессора
class Register:
    R0 = 0
    R1 = 1
    R2 = 2
    R3 = 3
    R4 = 4
    R5 = 5
    R6 = 6
    R7 = 7
    R8 = 8
    R9 = 9
    R10 = 10
    R11 = 11
    R12 = 12
    R13 = 13
    FP = 14   # Указатель кадра
    SP = 15   # Указатель стека

# Константы для режимов адресации
class AddrMode:
    REG = 0           # Регистровый (000)            Rn
    IMMEDIATE = 1     # Непосредственный (001)        #imm
    ABSOLUTE = 2      # Абсолютный (010)              [imm]
    BASE_OFFSET = 3   # Базовый со смещением (011)    [Rn + imm]
    POST_INC = 4      # Постинкрементный (100)        [Rn]+
    REG_INDIRECT = 5  # Регистровый косвенный (101)   [Rn]

# Константы форматов инструкций
class InstrFormat:
    A = 0    # Без операндов
    B = 1    # Ветвления и вызовы (24-бит смещение)
    C1 = 2   # Переменное число операндов
    C2 = 3   # Два операнда
    C3 = 4   # Один операнд

# Коды операций (Опкоды)
class Opcode:
    PUSHM = 0x11
    POPM = 0x12
    
    MOV = 0x20
    ADD = 0x21
    SUB = 0x22
    MUL = 0x23
    DIV = 0x24
    MOD = 0x25
    CMP = 0x26
    IN = 0x27
    OUT = 0x28
    
    SETL = 0x29
    SETG = 0x2A
    SETE = 0x2B
    
    JMP = 0x30
    BEQ = 0x31
    BNE = 0x32
    BLT = 0x33
    BGT = 0x34
    CALL = 0x35
    
    RET = 0x40
    IRET = 0x41
    EI = 0x42
    DI = 0x43
    HALT = 0x44

# Словарь для быстрого определения формата инструкции по опкоду
OPCODE_FORMAT = {
    Opcode.PUSHM: InstrFormat.C1,
    Opcode.POPM: InstrFormat.C1,
    Opcode.MOV: InstrFormat.C2,
    Opcode.ADD: InstrFormat.C2,
    Opcode.SUB: InstrFormat.C2,
    Opcode.MUL: InstrFormat.C2,
    Opcode.DIV: InstrFormat.C2,
    Opcode.MOD: InstrFormat.C2,
    Opcode.CMP: InstrFormat.C2,
    Opcode.IN: InstrFormat.C2,
    Opcode.OUT: InstrFormat.C2,
    Opcode.SETL: InstrFormat.C3,
    Opcode.SETG: InstrFormat.C3,
    Opcode.SETE: InstrFormat.C3,
    Opcode.JMP: InstrFormat.B,
    Opcode.BEQ: InstrFormat.B,
    Opcode.BNE: InstrFormat.B,
    Opcode.BLT: InstrFormat.B,
    Opcode.BGT: InstrFormat.B,
    Opcode.CALL: InstrFormat.B,
    Opcode.RET: InstrFormat.A,
    Opcode.IRET: InstrFormat.A,
    Opcode.EI: InstrFormat.A,
    Opcode.DI: InstrFormat.A,
    Opcode.HALT: InstrFormat.A,
}


# ==========================================
# 2. КЛАССЫ ДЛЯ ХРАНЕНИЯ ДАННЫХ
# ==========================================

# Операнд инструкции (дескриптор)
class Descriptor:
    def __init__(self):
        self.mode = 0       # Режим адресации
        self.reg = 0        # Регистр
        self.short = False  # Флаг короткого операнда (помещается в 8 бит)

# Представление инструкции в памяти
class Instruction:
    def __init__(self):
        self.opcode = 0
        self.operands = []    # Список объектов Descriptor
        self.ext_values = []  # Список числовых значений операндов (доп. слова)
        self.offset = 0       # Смещение для переходов (ветвления)
        self.regs = []        # Список регистров (для формата C1)
        self.func_addr = 0    # Зарезервировано (адрес функции; вызовы идут через CALL)
        self.address = 0      # Логический адрес инструкции


# ==========================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ОПЕРАНДОВ
# ==========================================

def has_value(mode):
    # Проверяет, требуется ли операнду числовое значение
    if mode == AddrMode.IMMEDIATE:
        return True
    if mode == AddrMode.ABSOLUTE:
        return True
    if mode == AddrMode.BASE_OFFSET:
        return True
    return False

def needs_extension(mode, is_short):
    # Проверяет, нужно ли выделять под значение целое 32-битное слово (не short)
    if has_value(mode):
        if not is_short:
            return True
    return False

def encode_descriptor(desc):
    # Упаковывает дескриптор в 1 байт: [mode:3 бита] [reg:4 бита] [short:1 бит]
    mode_bits = (desc.mode & 0x7) << 5
    reg_bits = (desc.reg & 0xF) << 1
    
    short_bit = 0
    if desc.short:
        short_bit = 1
        
    return mode_bits | reg_bits | short_bit

def decode_descriptor(byte_val):
    # Распаковывает 1 байт обратно в объект Descriptor
    desc = Descriptor()
    desc.mode = (byte_val >> 5) & 0x7
    desc.reg = (byte_val >> 1) & 0xF
    
    if (byte_val & 0x1) == 1:
        desc.short = True
    else:
        desc.short = False
        
    return desc


# ==========================================
# 4. РАБОТА СО ЗНАКОМ (Дополнительный код)
# ==========================================

def sign_extend_24(v):
    # Если установлен 24-й бит (0x800000), число отрицательное
    if (v & 0x800000) != 0:
        return v - 0x1000000
    return v

def pack_signed_24(v):
    # Перевод отрицательного числа в 24-битный дополнительный код
    if v < 0:
        v = v + 0x1000000
    return v & 0xFFFFFF

def sign_extend_8(v):
    # Если установлен 8-й бит (0x80), число отрицательное
    if (v & 0x80) != 0:
        return v - 0x100
    return v

def pack_signed_8(v):
    # Перевод отрицательного числа в 8-битный дополнительный код
    if v < 0:
        v = v + 0x100
    return v & 0xFF


# ==========================================
# 5. КОДИРОВАНИЕ: Instruction -> list[int] -> bytes
# ==========================================

def encode_instruction(instr):
    fmt = OPCODE_FORMAT[instr.opcode]
    words = []
    
    if fmt == InstrFormat.A:
        # Формат A: только опкод в старшем байте
        word = instr.opcode << 24
        words.append(word)

    elif fmt == InstrFormat.B:
        # Формат B: опкод и 24 бита смещения
        offset_packed = pack_signed_24(instr.offset)
        word = (instr.opcode << 24) | offset_packed
        words.append(word)

    elif fmt == InstrFormat.C2:
        # Формат C2: два операнда
        d1_enc = 0
        if len(instr.operands) > 0:
            d1_enc = encode_descriptor(instr.operands[0])
            
        d2_enc = 0
        if len(instr.operands) > 1:
            d2_enc = encode_descriptor(instr.operands[1])
            
        imm8 = 0
        val_index = 0
        
        # Ищем значение для поля imm8 (если хотя бы один операнд типа short)
        for desc in instr.operands:
            if has_value(desc.mode):
                if desc.short:
                    if val_index < len(instr.ext_values):
                        imm8 = pack_signed_8(instr.ext_values[val_index])
                val_index = val_index + 1
                
        word = (instr.opcode << 24) | (d1_enc << 16) | (d2_enc << 8) | imm8
        words.append(word)
        
        # Добавляем 32-битные слова расширения для операндов, которым они нужны
        val_index = 0
        for desc in instr.operands:
            if has_value(desc.mode):
                if not desc.short:
                    val = 0
                    if val_index < len(instr.ext_values):
                        val = instr.ext_values[val_index]
                    words.append(val & 0xFFFFFFFF)
                val_index = val_index + 1

    elif fmt == InstrFormat.C3:
        # Формат C3: один операнд
        d1_enc = 0
        if len(instr.operands) > 0:
            d1_enc = encode_descriptor(instr.operands[0])
            
        imm8 = 0
        if len(instr.operands) > 0:
            desc = instr.operands[0]
            if desc.short:
                if len(instr.ext_values) > 0:
                    imm8 = pack_signed_8(instr.ext_values[0])
                    
        word = (instr.opcode << 24) | (d1_enc << 16) | (imm8 << 8)
        words.append(word)
        
        if len(instr.operands) > 0:
            desc = instr.operands[0]
            if needs_extension(desc.mode, desc.short):
                val = 0
                if len(instr.ext_values) > 0:
                    val = instr.ext_values[0]
                words.append(val & 0xFFFFFFFF)

    elif fmt == InstrFormat.C1:
        # Формат C1: список регистров
        count = len(instr.regs)
        word = (instr.opcode << 24) | (count << 16)
        
        # Упаковываем первые 4 регистра в основное слово
        for i in range(count):
            if i == 4:
                break
            reg_val = instr.regs[i] & 0xF
            shift = 12 - (i * 4)
            word = word | (reg_val << shift)
            
        words.append(word)
        
        # Если регистров больше 4, пакуем их в дополнительные слова (по 8 штук на слово)
        if count > 4:
            remaining = count - 4
            offset = 4
            while remaining > 0:
                extra_word = 0
                for j in range(8):
                    if remaining == 0:
                        break
                    reg_val = instr.regs[offset] & 0xF
                    shift = 28 - (j * 4)
                    extra_word = extra_word | (reg_val << shift)
                    offset = offset + 1
                    remaining = remaining - 1
                words.append(extra_word)

    return words

def to_bytes(instructions):
    # Преобразуем список инструкций в сырой массив байтов
    result = bytearray()
    for instr in instructions:
        words = encode_instruction(instr)
        for word in words:
            # Ручное извлечение байтов из 32-битного слова (Big-Endian)
            b1 = (word >> 24) & 0xFF
            b2 = (word >> 16) & 0xFF
            b3 = (word >> 8) & 0xFF
            b4 = word & 0xFF
            result.append(b1)
            result.append(b2)
            result.append(b3)
            result.append(b4)
    return bytes(result)


# ==========================================
# 6. ДЕКОДИРОВАНИЕ: bytes -> list[Instruction]
# ==========================================

def read_word(data, pos):
    # Ручное чтение 32-битного слова из массива байтов (Big-Endian)
    b1 = data[pos]
    b2 = data[pos + 1]
    b3 = data[pos + 2]
    b4 = data[pos + 3]
    return (b1 << 24) | (b2 << 16) | (b3 << 8) | b4

def from_bytes(data):
    instructions = []
    pos = 0
    addr = 0

    while pos + 4 <= len(data):
        w0 = read_word(data, pos)
        opcode = (w0 >> 24) & 0xFF
        
        # Если опкод не распознан, пропускаем слово
        if opcode not in OPCODE_FORMAT:
            pos = pos + 4
            addr = addr + 1
            continue
            
        fmt = OPCODE_FORMAT[opcode]
        
        instr = Instruction()
        instr.opcode = opcode
        instr.address = addr
        
        words_consumed = 1

        if fmt == InstrFormat.A:
            pass # Операндов нет

        elif fmt == InstrFormat.B:
            offset_raw = w0 & 0xFFFFFF
            instr.offset = sign_extend_24(offset_raw)

        elif fmt == InstrFormat.C2:
            desc1_raw = (w0 >> 16) & 0xFF
            desc2_raw = (w0 >> 8) & 0xFF
            imm8_raw = w0 & 0xFF
            
            d1 = decode_descriptor(desc1_raw)
            d2 = decode_descriptor(desc2_raw)
            instr.operands.append(d1)
            instr.operands.append(d2)
            
            # Читаем значения для первого операнда
            if needs_extension(d1.mode, d1.short):
                ext_pos = pos + (words_consumed * 4)
                if ext_pos + 4 <= len(data):
                    val = read_word(data, ext_pos)
                    instr.ext_values.append(val)
                    words_consumed = words_consumed + 1
            elif has_value(d1.mode) and d1.short:
                instr.ext_values.append(sign_extend_8(imm8_raw))
                
            # Читаем значения для второго операнда
            if needs_extension(d2.mode, d2.short):
                ext_pos = pos + (words_consumed * 4)
                if ext_pos + 4 <= len(data):
                    val = read_word(data, ext_pos)
                    instr.ext_values.append(val)
                    words_consumed = words_consumed + 1
            elif has_value(d2.mode) and d2.short:
                instr.ext_values.append(sign_extend_8(imm8_raw))

        elif fmt == InstrFormat.C3:
            desc_raw = (w0 >> 16) & 0xFF
            imm8_raw = (w0 >> 8) & 0xFF
            
            d1 = decode_descriptor(desc_raw)
            instr.operands.append(d1)
            
            if needs_extension(d1.mode, d1.short):
                ext_pos = pos + (words_consumed * 4)
                if ext_pos + 4 <= len(data):
                    val = read_word(data, ext_pos)
                    instr.ext_values.append(val)
                    words_consumed = words_consumed + 1
            elif has_value(d1.mode) and d1.short:
                instr.ext_values.append(sign_extend_8(imm8_raw))

        elif fmt == InstrFormat.C1:
            count = (w0 >> 16) & 0xFF
            
            # Читаем до 4-х регистров из основного слова
            for i in range(count):
                if i == 4:
                    break
                shift = 12 - (i * 4)
                reg_val = (w0 >> shift) & 0xF
                instr.regs.append(reg_val)
                
            remaining = count - 4
            
            # Читаем остальные регистры из дополнительных слов
            while remaining > 0:
                ext_pos = pos + (words_consumed * 4)
                if ext_pos + 4 > len(data):
                    break
                    
                extra_word = read_word(data, ext_pos)
                words_consumed = words_consumed + 1
                
                for j in range(8):
                    if remaining == 0:
                        break
                    shift = 28 - (j * 4)
                    reg_val = (extra_word >> shift) & 0xF
                    instr.regs.append(reg_val)
                    remaining = remaining - 1

        # Сдвигаемся к следующей инструкции
        pos = pos + (words_consumed * 4)
        addr = addr + words_consumed
        instructions.append(instr)

    return instructions