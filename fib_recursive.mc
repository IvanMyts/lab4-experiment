; Recursive Fibonacci, (fib 10) = 55
;
; Conventions:
;   D1 = argument n
;   D0 = return value
;   port 0 = console output
;
; ISA reminders (см. README, раздел "Система команд"):
;   Group 1 (variable-count, формат C1): high nibble = opcode, low nibble = count,
;     далее 8-битные дескрипторы (3 шт. в первом слове).
;       MOV  = 0x0_   MOVA = 0x1_   PUSH = 0x2_   POP  = 0x3_   POLY = 0x4_
;   Group 2 (two-operand, формат C2): полный 8-битный opcode + два дескриптора + pad.
;       ADD=0x50  SUB=0x51  MUL=0x52  DIV=0x53  MOD=0x54
;       CMP=0x55  IN =0x56  OUT=0x57  AND=0x58  OR =0x59  SHL=0x5A  SHR=0x5B
;   Group 3 (branches/CALL, формат B): opcode + 24-битное знаковое PC-relative смещение,
;     отсчитываемое от адреса слова, следующего за инструкцией ветвления.
;       JMP=0x60  BEQ=0x61  BLT=0x62  BGT=0x63  CALL=0x64
;   Group 4 (no operands, формат A): RET=0x70  IRET=0x71  EI=0x72  DI=0x73  HALT=0x74
;
; Descriptor reminders (8 бит):
;   register Dn/An :  0000 RRR 0     (D0=0x00 D1=0x02 D2=0x04 D3=0x06 ...)
;   immediate #i32 :  0001 000 0 = 0x10  + одно 32-битное слово расширения
;   absolute  [i32]:  0101 000 0 = 0x50  + одно 32-битное слово расширения
;   [An]           :  0010 AAA 0
;   [An]+          :  0011 AAA 0
;   -[An]          :  0100 AAA 0
;   [An, Dn]       :  11 AAA DDD     (база + индекс, 1 байт без расширений)
;   [An + imm3]    :  10 AAA III     (FP-relative до 7, без расширений)
;   [An + imm32]   :  0110 AAA 0     + 32-битное слово расширения

; ===== main =====
0100: 0x02100200  ; MOV  #10, D1                 ; desc1=#i32(0x10) desc2=D1(0x02)
0101: 0x0000000A  ;   ext: #10
0102: 0x64000003  ; CALL fib                     ; offset = +3  (-> 0x0106)
0103: 0x57001000  ; OUT  D0, 0                   ; desc1=D0(0x00) desc2=#i32(0x10)
0104: 0x00000000  ;   ext: port 0
0105: 0x74000000  ; HALT                         ; stop simulation

; ===== fib(n): принимает n в D1, возвращает результат в D0 =====
0106: 0x55021000  ; CMP  D1, #2                  ; desc1=D1 desc2=#i32
0107: 0x00000002  ;   ext: #2
0108: 0x62000010  ; BLT  fib_base                ; offset = +16 (-> 0x0119)
0109: 0x21020000  ; PUSH D1                      ; count=1 desc1=D1
010A: 0x02020400  ; MOV  D1, D2                  ; desc1=D1 desc2=D2
010B: 0x51100400  ; SUB  #1, D2                  ; desc1=#i32 desc2=D2
010C: 0x00000001  ;   ext: #1
010D: 0x02040200  ; MOV  D2, D1                  ; desc1=D2 desc2=D1
010E: 0x64FFFFF7  ; CALL fib                     ; offset = -9  (-> 0x0106)
010F: 0x31020000  ; POP  D1                      ; count=1 desc1=D1
0110: 0x21000000  ; PUSH D0                      ; count=1 desc1=D0
0111: 0x02020400  ; MOV  D1, D2                  ; desc1=D1 desc2=D2
0112: 0x51100400  ; SUB  #2, D2                  ; desc1=#i32 desc2=D2
0113: 0x00000002  ;   ext: #2
0114: 0x02040200  ; MOV  D2, D1                  ; desc1=D2 desc2=D1
0115: 0x64FFFFF0  ; CALL fib                     ; offset = -16 (-> 0x0106)
0116: 0x31040000  ; POP  D2                      ; count=1 desc1=D2
0117: 0x50040000  ; ADD  D2, D0                  ; desc1=D2 desc2=D0
0118: 0x70000000  ; RET

; ===== fib_base: n < 2 -> возвращаем n =====
0119: 0x02020000  ; MOV  D1, D0                  ; desc1=D1 desc2=D0
011A: 0x70000000  ; RET
