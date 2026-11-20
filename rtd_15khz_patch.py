#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""
rtd_15khz_patch.py - lower the input frequency limits in Realtek monitor
firmware dumps so the monitor accepts 15 kHz video.

It patches a COPY of your own firmware dump. The original file is never changed.

What it does:
  1. Checks the dump size and warns about wrapped (mirrored) reads.
  2. Searches the code for the input range check by its instruction pattern
     (not by fixed offsets) and lowers the minimum H frequency to 10.0 kHz.
  3. Where the same code also has a minimum V frequency, lowers it to 23.0 Hz.
  4. Where the firmware has an "accept, but warn" path for signals it considers
     not recommended, disables it. On the HP E190i that path kept showing an
     "Input Signal Out of Range" box over a working 15 kHz VGA picture and locked
     every menu except the input menu.
  5. Where the DVI input drops HDMI sources (HP E190i: an HDMI stream is reported as an input
     type the rest of the firmware does not handle, so "No Input Signal"), handles them as DVI.
  6. Lists the 15 kHz entries in the VGA mode table.
  7. Optional (--wide400 or --smooth400): samples the 31.5 kHz / 70 Hz 400-line mode
     (DOS text mode and 320x200 games) 1280 wide (--wide400: crisp, no horizontal scaling) or
     1440 wide and scaled down to 1280 (--smooth400: even, slightly smoothed).
  8. Optional (--ddc-debug, HP E190i only): adds DDC/CI code 0xE0 for reading the scaler's
     registers live over the VGA cable with ddc_probe.py.
  9. Optional (--aspect43, HP E190i and ThinkVision LT1913p): shows modes below 960 lines as 4:3 (1280x960 with
     black bars) instead of stretched to the 5:4 panel. Native 1280x1024 is unchanged.
 10. Optional (--manual-auto, HP E190i only): VGA auto adjust runs only when you ask for it
     (menu or DDC/CI), not by itself whenever a new mode appears.

It refuses to write anything if the patterns are missing or look unusual.

Tested only on: HP EliteDisplay E190i (patched; 15 kHz works on VGA and DisplayPort with no
warning box, menus and auto adjust work; HDMI sources work on DVI; --wide400, --smooth400, --ddc-debug, --aspect43 and --manual-auto work)
and Lenovo ThinkVision LT1913p (already has the low limits, nothing to patch).

Usage:
  python3 rtd_15khz_patch.py dump.bin                 # analyse only
  python3 rtd_15khz_patch.py dump.bin -o patched.bin  # write patched copy
  python3 rtd_15khz_patch.py dump.bin -o patched.bin --wide400     # or --smooth400
  python3 rtd_15khz_patch.py dump.bin --trim -o patched.bin   # fix a wrapped read
  python3 rtd_15khz_patch.py dump.bin -o patched.bin --ddc-debug  # HP E190i: live debug code
  python3 rtd_15khz_patch.py dump.bin -o patched.bin --aspect43   # HP E190i: 4:3 instead of 5:4
  python3 rtd_15khz_patch.py dump.bin -o patched.bin --manual-auto  # HP E190i: no automatic auto adjust

NO WARRANTY. A bad flash can leave a monitor dark. Keep your original dump.
"""
import argparse
import re
import struct
import sys

H_MIN_NEW = 100   # 10.0 kHz, in 0.1 kHz units (same as the ThinkVision LT1913p)
V_MIN_NEW = 230   # 23.0 Hz, in 0.1 Hz units
MAX_RANGE_CHECKS = 4

# 8051 code for "is value (16 bit, in two direct RAM bytes) inside [min, max]":
#   CLR C ; MOV A,lo ; SUBB A,#min_lo ; MOV A,hi ; SUBB A,#min_hi ; JC/JNC out
#   SETB C ; MOV A,lo ; SUBB A,#max_lo ; MOV A,hi ; SUBB A,#max_hi ; JC/JNC
RANGE = re.compile(
    rb'\xC3\xE5(.)\x94(.)\xE5(.)\x94(.)[\x40\x50].'
    rb'\xD3\xE5(.)\x94(.)\xE5(.)\x94(.)[\x40\x50].', re.S)

# Standalone "reject if V below minimum":
#   CLR C ; MOV A,lo ; SUBB A,#min_lo ; MOV A,hi ; SUBB A,#min_hi ; JNC +1 ; RET
V_REJECT = re.compile(rb'\xC3\xE5(.)\x94(.)\xE5(.)\x94(.)\x50\x01\x22', re.S)

# "Accept, but flag as out of range" (HP E190i, bank 2 at 0xEB20). A signal that passes the
# range check but is "not recommended" gets a flag bit set. While that bit is set the OSD keeps
# showing "Input Signal Out of Range / Change Settings to ..." and only the input menu works.
#   LCALL soft_check ; JNC strict (+0x11)
#   MOV DPTR,#flags ; MOVX A,@DPTR ; ORL A,#bit ; MOVX @DPTR,A      <- sets the warning flag
#   LCALL get_state ; CJNE R7,#n,+2 ; SETB C ; RET ; CLR C ; RET
#   strict: LCALL check ; MOV A,R7 ; JZ +2 ; SETB C ; RET ; CLR C ; RET
# The fix turns the JNC into SJMP, so every signal goes through the normal (strict) check.
SOFT_WARN = re.compile(
    rb'\x12..([\x50\x80])\x11\x90..\xE0\x44.\xF0\x12..\xBF.\x02\xD3\x22\xC3\x22'
    rb'\x12..\xEF\x60\x02\xD3\x22\xC3\x22', re.S)

# Interlace reject in the strict check (HP E190i, bank 2 at 0xA003). VGA and DVI (input types 0
# and 2) reject any signal the hardware flags as interlaced; other inputs only when 2 x Vtotal > 1024.
#   LCALL input_type ; MOV A,R7 ; JZ +6 ; LCALL input_type ; CJNE R7,#2,other
#   MOV R7,flags ; MOV A,R7 ; RRC A ; RRC A ; ANL A,#3F ; JNB ACC.0,other ; MOV R7,#1 ; RET
#   other: MOV R7,flags ...
# The fix replaces the first LCALL with SJMP other, so all inputs use the lenient rule.
# Needed together with SOFT_WARN: after that fix every signal goes through this check.
INTERLACE = re.compile(
    rb'(?:\x12(..)|\x80\x17\x00)\xEF\x60\x06\x12..\xBF\x02\x0D\xAF(.)\xEF\x13\x13\x54\x3F'
    rb'\x30\xE0\x03\x7F\x01\x22\xAF', re.S)

# DVI input with HDMI sources (HP E190i). The DVI scan (bank 0 at 0x5F84) checks the receiver's
# "HDMI format" flag and, when set, reports the input as type 3 (HDMI). The rest of the HP firmware
# only knows types 0 (VGA), 2 (DVI) and 4 (DP), so a type 3 input is dropped as "No Input Signal".
#   LCALL hdmi_detect ; MOV A,R7 ; XRL A,#3 ; JNZ dvi_mode ; MOV DPTR,#x ; MOV A,#2 ; MOVX ; MOV R6,A ; LCALL
# The fix makes the JNZ an SJMP: HDMI streams are handled as DVI (type 2).
HDMI_SCAN = re.compile(rb'\x12..\xEF\x64\x03([\x70\x80])\x2C\x90..\x74\x02\xF0\xFE\x12', re.S)

# ...and the DVI "is there a signal" check (bank 0 at 0x9549) rejects a type 2 input whose HDMI
# format flag (reg 0x02CB bit 0) is set:
#   MOV DPTR,#x ; MOVX ; CJNE A,#3,dvi ; (type 3 part) ; dvi: MOV DPTR,#02CB ; MOVX ; JNB ACC.0,ok ; CLR C ; RET
# The fix replaces the JNB with SJMP ok.
HDMI_FLAG = re.compile(
    rb'\x90..\xE0\xB4\x03\x09\x90\x02\xCB\xE0\x20\xE0\x0B\xC3\x22\x90\x02\xCB\xE0'
    rb'(\x30\xE0\x02|\x80\x03\x00)\xC3\x22', re.S)

# --ddc-debug (HP E190i only): DDC/CI "Get/Set VCP" switch over the 47 published VCP codes.
VCP_TABLE = re.compile(rb'\x12..((?:..[\x02])(?:..[\x04])(?:..[\x05])(?:..[\x06])(?:..[\x08])'
                       rb'(?:..[\x0B])(?:..[\x0C])(?:..[\x0E])(?:..[\x10])(?:..[\x12]))', re.S)

ENTRY = struct.Struct('>BHHHHBBHHHH')  # pol, w, h, hfreq, vfreq, htol, vtol, htotal, vtotal, hstart, vstart


def find_range_checks(d):
    """Return H range checks (min 15-30 kHz, max 50-120 kHz) and the V range
    check that directly follows each one, if any."""
    out = []
    v_starts = set()
    for m in RANGE.finditer(d):
        if m.start() in v_starts:   # this is the V half of a check already found
            continue
        lo, mn_lo, hi, mn_hi, lo2, mx_lo, hi2, mx_hi = (b[0] for b in m.groups())
        if not (lo == lo2 and hi == hi2 and hi == lo - 1):
            continue
        mn, mx = mn_hi << 8 | mn_lo, mx_hi << 8 | mx_lo
        if not (100 <= mn <= 300 and 500 <= mx <= 1200):
            continue
        # the min immediates sit at start+4 (low byte) and start+8 (high byte)
        chk = {'pos': m.start(), 'min': mn, 'max': mx,
               'min_lo_at': m.start() + 4, 'min_hi_at': m.start() + 8, 'v': None}
        v = RANGE.match(d, m.end())
        if v:
            vl, vmn_lo, vh, vmn_hi, vl2, vmx_lo, vh2, vmx_hi = (b[0] for b in v.groups())
            vmn, vmx = vmn_hi << 8 | vmn_lo, vmx_hi << 8 | vmx_lo
            if vl == vl2 and vh == vh2 and vh == vl - 1 and 150 <= vmn <= 600 and 600 <= vmx <= 1000:
                v_starts.add(v.start())
                chk['v'] = {'min': vmn, 'max': vmx,
                            'min_lo_at': v.start() + 4, 'min_hi_at': v.start() + 8}
        # a separate V minimum reject shortly after the H check (HP E190i: 49.5 Hz)
        chk['vrej'] = None
        for vr in V_REJECT.finditer(d, m.end(), m.end() + 0x80):
            vl, vmn_lo, vh, vmn_hi = (b[0] for b in vr.groups())
            vmn = vmn_hi << 8 | vmn_lo
            if vh == vl - 1 and 150 <= vmn <= 600:
                chk['vrej'] = {'min': vmn, 'min_lo_at': vr.start() + 4, 'min_hi_at': vr.start() + 8}
            break
        out.append(chk)
    return out


def find_mode_table(d):
    """Locate the VGA mode table by its 720x400 @ 31.5 kHz / 70 Hz entry."""
    # the 720x400 entry; 1280 / 1440 wide if already patched with --wide400 / --smooth400
    hits = [d.find(struct.pack('>HHHH', w, 400, 315, 700)) for w in (720, 1280, 1440)]
    hits = [h for h in hits if h >= 1]
    m = min(hits) if hits else -1
    if m < 1:
        return None, []
    ok = lambda e: 200 <= e[1] <= 4096 and 150 <= e[2] <= 2400 and 50 < e[3] < 2000
    start = m - 1
    while start >= ENTRY.size and ok(ENTRY.unpack_from(d, start - ENTRY.size)):
        start -= ENTRY.size
    entries = []
    o = start
    while o + ENTRY.size <= len(d) and ok(ENTRY.unpack_from(d, o)):
        entries.append((o, ENTRY.unpack_from(d, o)))
        o += ENTRY.size
    return start, entries


# --manual-auto (HP E190i only): when a VGA mode is ready, the firmware asks "has this mode never
# been auto adjusted?" and, if so, queues the auto adjust event (0x0B). The flag is set for every new
# mode slot and cleared only by a successful auto adjust, so a failed run repeats at every resync.
#   LCALL is_vga ; MOV A,R7 ; JNZ skip ; LCALL needs_auto ; JNC skip ; MOV DPTR,#event ; MOV A,#0Bh ; MOVX
# The fix replaces LCALL needs_auto with CLR C ; NOP ; NOP. The menu item and DDC/CI VCP 0x1E
# (auto setup) call the auto adjust routine directly and keep working.
AUTO_TRIGGER = re.compile(rb'\x12..\xEF\x70\x0E(\x12..|\xC3\x00\x00)\x50\x09\x90..\x74\x0B\xF0', re.S)


def manual_auto_changes(d):
    m = list(AUTO_TRIGGER.finditer(d))
    if len(m) != 1:
        sys.exit('--manual-auto: auto adjust trigger matched %d times (expected 1), nothing written.' % len(m))
    pos = m[0].start(1)
    if d[pos] == 0xC3:
        print('--manual-auto: already present.')
        return []
    return [(pos, 0xC3, 'auto adjust only on request (menu / DDC/CI), not on new modes, at 0x%05X' % pos),
            (pos + 1, 0x00, None), (pos + 2, 0x00, None)]


def ddc_debug_changes(d):
    """Add DDC/CI VCP code 0xE0 for live debugging (see ddc_probe.py peek/watch):
    'set E0 = address' remembers an address, 'get E0' returns the byte at that address
    (current value) and the address itself (maximum value). Only tested on the HP E190i, so
    every routine the new code jumps to is checked byte for byte first."""
    m = list(VCP_TABLE.finditer(d))
    if len(m) != 1:
        sys.exit('--ddc-debug: DDC/CI VCP table matched %d times (expected 1), nothing written.' % len(m))
    j = m[0].start(1)
    bank = j & ~0xFFFF
    while not (d[j] == 0 and d[j + 1] == 0):
        j += 3
    defpos = j + 2
    dflt = d[defpos] << 8 | d[defpos + 1]
    get_done, done, stub, mem = 0x23B7, 0x23DD, 0xE100, 0xFEFA   # HP E190i layout
    if dflt == stub:
        print('--ddc-debug: already present.')
        return []
    expect = {dflt: '90fc20e0ffb40108', get_done: '12bd5e8021', done: '90fc20e0b40111'}
    for addr, hexs in expect.items():
        if d[bank + addr:bank + addr + len(hexs) // 2] != bytes.fromhex(hexs):
            sys.exit('--ddc-debug: code at %X:%04X is not what the HP E190i has, nothing written.' % (bank >> 16, addr))
    if any(x != 0xFF for x in d[bank + stub - 16:bank + stub + 0x80]):
        sys.exit('--ddc-debug: no free space at %X:%04X, nothing written.' % (bank >> 16, stub))
    if re.search(bytes([0x90, mem >> 8]) + b'[\xFA\xFB]', d):
        sys.exit('--ddc-debug: RAM at 0x%04X is used by the firmware, nothing written.' % mem)

    def lj(a):
        return bytes([0x02, a >> 8, a & 0xFF])
    m_hi, m_lo = mem >> 8, mem & 0xFF
    code = (bytes.fromhex('90fc21e0' '64e0' '6003') + lj(dflt) +          # VCP code != E0 -> original
            bytes.fromhex('90fc20e0' 'b40313') +                           # opcode != 03 (set) -> get part
            bytes.fromhex('90fc22e0fea3e0ff') + bytes([0x90, m_hi, m_lo]) +
            bytes.fromhex('eef0a3eff0') + lj(done) +                       # set: store address, finish
            bytes.fromhex('b40115') + bytes([0x90, m_hi, m_lo]) +          # opcode != 01 (get) -> original
            bytes.fromhex('e0fca3e0fd' '8c838d82' 'e0fb7a007f00') + lj(get_done) +  # get: reply byte
            lj(dflt))
    assert len(code) == 64
    out = [(bank + stub + i, b, None) for i, b in enumerate(code)]
    out += [(defpos, stub >> 8, 'DDC/CI debug VCP 0xE0 (code at %X:%04X, address kept at 0x%04X)' % (bank >> 16, stub, mem)),
            (defpos + 1, stub & 0xFF, None)]
    return out


# --aspect43: bank 0 layouts of the two tested firmwares. Both are built from the same Realtek code
# (compare the RTD2660 reference: stModeInfo / stDisplayInfo / CAdjustIVS2DVSDelay), with the
# variables one byte apart. Every byte the patch relies on is checked before anything is written.
A43_LAYOUTS = [
    dict(name='HP E190i', var=0x00,       # IDATA addresses below are for var = 0 (HP)
         setup=0xDC80, setup_bytes='12d91812e63a12e5ca', defaults_fn=0xE63A, defaults=0xD58D, tail=0xB994,
         start_at=0x59F5, delay=0x1950, bg_at=0x2BBE, lmul=0xB03F),
    dict(name='ThinkVision LT1913p', var=-1,
         setup=0xBDFF, setup_bytes='12bafc12c6d312c669', defaults_fn=0xC6D3, defaults=0xB750, tail=0xD1DE,
         start_at=0x5A27, delay=0x10C6, bg_at=0x2694, lmul=0xCD10),
]
A43_CODE = 0xF000
A43_VMIN = 1036   # fewest output lines per frame allowed: 1024 panel lines + 6 start + 6 blank


def a43_layout_bytes(L):
    """What the firmware must contain at each address (HP variable addresses shifted by L['var'])."""
    v = lambda x: x + L['var']
    w = lambda a: bytes([a >> 8, a & 0xFF])
    j = lambda op, a: bytes([op]) + w(a)
    dv, pv, ds, t = v(0x45), v(0x47), v(0x49), v(0x68)
    bg = (bytes.fromhex('90002a740ef0a3eff0' '90002a740ff0a3eef0' '90002a7421f0') +
          bytes([0xE5, ds + 1, 0x24, 0xFC, 0xA3, 0xF0, 0xE5, ds + 1, 0x25, pv + 1, 0xF5, t + 1,
                 0xE5, ds, 0x35, pv, 0xF5, t]) +
          bytes.fromhex('90002a7414f0a3') + bytes([0xE5, t, 0xF0]) +
          bytes.fromhex('90002a7415f0a3') + bytes([0xE5, t + 1, 0xF0]) + b'\xED\x24\xFC')
    return {
        L['setup']: bytes.fromhex(L['setup_bytes']),
        L['defaults_fn']: j(0x12, L['defaults']) + j(0x02, L['tail']),
        L['defaults'] + 0x18: bytes([0x75, dv, 4, 0x75, dv + 1, 0, 0x75, pv, 4, 0x75, pv + 1, 0]),
        L['start_at']: bytes([0x50, 0x06, 0x75, ds, 0, 0x75, ds + 1, 6]) + j(0x12, L['delay']),
        L['bg_at']: bg,
        L['lmul']: bytes.fromhex('e88ff0a4cc8bf0a4'),
        L['delay']: bytes([0xAE, v(0x3B), 0xAF, v(0x3C)]),
    }


def aspect43_code(L):
    """Three routines at A43_CODE and the three hooks that call them.
    S1 (mode setup, after the panel defaults): DV height = 960 if IVHeight < 960 and
        IVTotal * 960 >= A43_VMIN * IVHeight (enough vertical blanking for a 960-line picture).
    S2 (before the IVS-to-DVS delay is computed): off = (panel V - DV height) / 2;
        DV start (start of the picture) = max(DV start, 6 + off).
    S3 (display window): background (panel frame) starts at DV start - off, 1024 lines long."""
    v = lambda x: x + L['var']
    w = lambda a: bytes([a >> 8, a & 0xFF])
    dv, pv, ds, ivt, ivh, t = v(0x45), v(0x47), v(0x49), v(0x33), v(0x35), v(0x68)
    lmul = b'\x12' + w(L['lmul'])
    s1 = (b'\x12' + w(L['defaults']) +
          bytes([0xC3, 0xE5, ivh + 1, 0x94, 0xC0, 0xE5, ivh, 0x94, 0x03, 0x50, 0x4D,   # IVHeight >= 960
                 0xE5, ivh, 0x45, ivh + 1, 0x60, 0x47,                                 # IVHeight == 0
                 0x7C, 0, 0x7D, 0, 0xAE, ivt, 0xAF, ivt + 1, 0x78, 0, 0x79, 0, 0x7A, 0x03, 0x7B, 0xC0]) +
          lmul + bytes.fromhex('c004c005c006c007') +
          bytes([0x7C, 0, 0x7D, 0, 0xAE, ivh, 0xAF, ivh + 1,
                 0x78, 0, 0x79, 0, 0x7A, A43_VMIN >> 8, 0x7B, A43_VMIN & 0xFF]) +
          lmul + bytes.fromhex('d003d002d001d000' 'c3eb9fea9ee99de89c' '4006') +
          bytes([0x75, dv, 0x03, 0x75, dv + 1, 0xC0]) + b'\x02' + w(L['tail']))
    s2 = (bytes([0xC3, 0xE5, pv + 1, 0x95, dv + 1, 0xFF, 0xE5, pv, 0x95, dv, 0xFE, 0x4F, 0x60, 0x1C]) +
          bytes.fromhex('eec313feef13ff' 'ef2406ffee3400fe') +
          bytes([0xC3, 0xE5, ds + 1, 0x9F, 0xE5, ds, 0x9E, 0x50, 0x04, 0x8E, ds, 0x8F, ds + 1]) +
          b'\x02' + w(L['delay']))
    s3 = (bytes([0xC0, 0x05, 0xC3, 0xE5, pv + 1, 0x95, dv + 1, 0xFD, 0xE5, pv, 0x95, dv]) +
          bytes.fromhex('c313fced13fd') +
          bytes([0xC3, 0xE5, ds + 1, 0x9D, 0xFF, 0xE5, ds, 0x9C, 0xFE]) +
          bytes.fromhex('90002a740ef0a3eef0' '90002a740ff0a3eff0' '90002a7421f0ef24fca3f0') +
          bytes([0xEF, 0x25, pv + 1, 0xF5, t + 1, 0xEE, 0x35, pv, 0xF5, t]) +
          bytes.fromhex('90002a7414f0a3') + bytes([0xE5, t, 0xF0]) +
          bytes.fromhex('90002a7415f0a3') + bytes([0xE5, t + 1, 0xF0]) +
          bytes([0xAF, ds, 0xAE, ds + 1, 0xD0, 0x05]) + b'\x02' + w(L['bg_at'] + 0x3E))
    assert len(s1) == 0x5E and len(s2) == 0x2D
    p1, p2, p3 = A43_CODE, A43_CODE + len(s1), A43_CODE + len(s1) + len(s2)
    hooks = [(L['setup'] + 3, b'\x12' + w(p1)), (L['start_at'] + 8, b'\x12' + w(p2)), (L['bg_at'], b'\x02' + w(p3))]
    return s1 + s2 + s3, hooks


def aspect43_changes(d):
    """Show every mode with fewer than 960 lines as 4:3 (1280x960, 32 black lines top and
    bottom) instead of stretched to the 5:4 panel. Modes whose timing leaves too little
    vertical blanking for a 960-line picture (800x600@60, 1024x768) stay as they are."""
    for L in A43_LAYOUTS:
        code, hooks = aspect43_code(L)
        if bytes(d[A43_CODE:A43_CODE + len(code)]) == code and all(d[p:p + 3] == h for p, h in hooks):
            print('--aspect43: already present (%s layout).' % L['name'])
            return []
        if all(d[a:a + len(b)] == b for a, b in a43_layout_bytes(L).items()):
            break
    else:
        sys.exit('--aspect43: the display code is not what the HP E190i or ThinkVision LT1913p has, nothing written.')
    if any(x != 0xFF for x in d[A43_CODE - 16:A43_CODE + 0x100]):
        sys.exit('--aspect43: no free space at 0:%04X, nothing written.' % A43_CODE)
    out = [(A43_CODE + i, b, None) for i, b in enumerate(code)]
    for p, h in hooks:
        out += [(p + i, b, None) for i, b in enumerate(h)]
    out[0] = (A43_CODE, code[0], '4:3 picture (1280x960) for modes below 960 lines, %s layout (code at 0:%04X)'
              % (L['name'], A43_CODE))
    return out


def khz(v):
    return '%.1f' % (v / 10)


def main():
    ap = argparse.ArgumentParser(description='Lower the input frequency limits in a Realtek monitor firmware dump for 15 kHz video.')
    ap.add_argument('dump', help='firmware dump read from the monitor')
    ap.add_argument('-o', '--out', help='write the patched copy here (omit to only analyse)')
    m400 = ap.add_mutually_exclusive_group()
    m400.add_argument('--wide400', action='store_true',
                      help='sample the 70 Hz 400-line mode 1280 wide: crisp 320x200 games, slightly uneven text')
    m400.add_argument('--smooth400', action='store_true',
                      help='sample the 70 Hz 400-line mode 1440 wide and scale to 1280: even, slightly smoothed text and games')
    ap.add_argument('--ddc-debug', action='store_true',
                    help='HP E190i only: add DDC/CI debug code 0xE0 to read the scaler live (see ddc_probe.py)')
    ap.add_argument('--aspect43', action='store_true',
                    help='HP E190i / ThinkVision LT1913p: show modes below 960 lines as 4:3 (1280x960 letterbox) instead of 5:4')
    ap.add_argument('--manual-auto', action='store_true',
                    help='HP E190i only: run VGA auto adjust only from the menu or DDC/CI, never by itself on new modes')
    ap.add_argument('--trim', action='store_true',
                    help='if the dump is a wrapped read (second half = first half), use only the first half')
    a = ap.parse_args()

    d = bytearray(open(a.dump, 'rb').read())
    print('File: %s, %d bytes (%d KB)' % (a.dump, len(d), len(d) // 1024))

    # --- size checks ---
    if len(d) & (len(d) - 1):
        sys.exit('ERROR: size is not a power of two. Is this a complete dump?')
    half = len(d) // 2
    if len(d) >= 0x20000 and d[:half] == d[half:]:
        if a.trim:
            d = d[:half]
            print('Wrapped read detected, using the first %d KB.' % (half // 1024))
        else:
            print('WARNING: the second half is a copy of the first. The chip is probably only %d KB\n'
                  '         and your programmer has the wrong size for it. Fix the size before writing,\n'
                  '         and rerun with --trim.' % (half // 1024))
            if a.out:
                sys.exit('Stopping, nothing written.')
    if d.count(0xFF) > len(d) * 0.95 or d.count(0x00) > len(d) * 0.95:
        sys.exit('ERROR: the dump is almost empty. The read probably failed.')

    # --- range checks ---
    checks = find_range_checks(d)
    print('\nInput range checks found: %d' % len(checks))
    if not checks:
        sys.exit('No matching range check. This firmware is different, nothing written.')
    if len(checks) > MAX_RANGE_CHECKS:
        sys.exit('Unexpectedly many matches (%d). Not safe to patch, nothing written.' % len(checks))

    changes = []
    for c in checks:
        line = '  at 0x%05X: H %s - %s kHz' % (c['pos'], khz(c['min']), khz(c['max']))
        if c['v']:
            line += ', V %s - %s Hz' % (khz(c['v']['min']), khz(c['v']['max']))
        if c['vrej']:
            line += ', rejects V below %s Hz' % khz(c['vrej']['min'])
        print(line)
        # only the reject checks (min 15-30 kHz); the 11 kHz one is a softer warning check
        if 150 <= c['min'] <= 300:
            changes.append((c['min_lo_at'], H_MIN_NEW & 0xFF, 'H min %s -> %s kHz' % (khz(c['min']), khz(H_MIN_NEW))))
            changes.append((c['min_hi_at'], H_MIN_NEW >> 8, None))
        if c['v'] and c['v']['min'] > V_MIN_NEW and 150 <= c['min'] <= 300:
            changes.append((c['v']['min_lo_at'], V_MIN_NEW & 0xFF, 'V min %s -> %s Hz' % (khz(c['v']['min']), khz(V_MIN_NEW))))
            changes.append((c['v']['min_hi_at'], V_MIN_NEW >> 8, None))
        if c['vrej'] and c['vrej']['min'] > V_MIN_NEW and 150 <= c['min'] <= 300:
            changes.append((c['vrej']['min_lo_at'], V_MIN_NEW & 0xFF, 'V reject below %s -> %s Hz' % (khz(c['vrej']['min']), khz(V_MIN_NEW))))
            changes.append((c['vrej']['min_hi_at'], V_MIN_NEW >> 8, None))

    # --- "out of range" warning that is shown over a working picture ---
    warn = list(SOFT_WARN.finditer(d))
    if not warn:
        print('\nOut-of-range warning path: not found (not needed on every monitor).')
    elif len(warn) > 1:
        sys.exit('Out-of-range warning path matched %d times. Not safe to patch, nothing written.' % len(warn))
    else:
        jpos = warn[0].start() + 3
        if d[jpos] == 0x80:
            print('\nOut-of-range warning path at 0x%05X: already disabled.' % jpos)
        else:
            print('\nOut-of-range warning path at 0x%05X.' % jpos)
            changes.append((jpos, 0x80, 'disable the recurring "Input Signal Out of Range" box (and menu lock) at 0x%05X' % jpos))

        # with the warning path gone, every signal reaches the strict check and its interlace reject
        il = list(INTERLACE.finditer(d))
        if len(il) != 1:
            sys.exit('Interlace check matched %d times (expected 1). Not safe to patch, nothing written.' % len(il))
        ipos = il[0].start()
        if d[ipos] == 0x80:
            print('Interlace reject at 0x%05X: already relaxed.' % ipos)
        else:
            print('Interlace reject at 0x%05X.' % ipos)
            changes.append((ipos, 0x80, 'let VGA/DVI use the same interlace rule as DisplayPort at 0x%05X' % ipos))
            changes.append((ipos + 1, 0x17, None))
            changes.append((ipos + 2, 0x00, None))

    # --- DVI input with HDMI sources ---
    scan, flag = list(HDMI_SCAN.finditer(d)), list(HDMI_FLAG.finditer(d))
    if not scan:
        # the HP quirk is in the scan; firmwares without it (e.g. ThinkVision) handle HDMI themselves
        print('\nDVI/HDMI handling: not found (not needed on every monitor).')
    elif len(scan) != 1 or len(flag) != 1:
        sys.exit('DVI/HDMI patterns matched %d / %d times (expected 1 / 1). Not safe to patch, nothing written.'
                 % (len(scan), len(flag)))
    else:
        spos, fpos = scan[0].start(1), flag[0].start(1)
        if d[spos] == 0x80 and d[fpos] == 0x80:
            print('\nDVI/HDMI handling at 0x%05X / 0x%05X: already patched.' % (spos, fpos))
        else:
            print('\nDVI/HDMI handling at 0x%05X / 0x%05X.' % (spos, fpos))
            changes.append((spos, 0x80, 'accept HDMI sources on the DVI input (handled as DVI) at 0x%05X / 0x%05X' % (spos, fpos)))
            changes += [(fpos, 0x80, None), (fpos + 1, 0x03, None), (fpos + 2, 0x00, None)]

    if a.ddc_debug:
        changes += ddc_debug_changes(d)
    if a.aspect43:
        changes += aspect43_changes(d)
    if a.manual_auto:
        changes += manual_auto_changes(d)

    # --- mode table ---
    start, entries = find_mode_table(d)
    if start is None:
        print('\nVGA mode table: not found.')
    else:
        print('\nVGA mode table at 0x%05X, %d entries.' % (start, len(entries)))
        low = [(o, e) for o, e in entries if e[3] < 200]
        if low:
            print('15 kHz entries:')
            for o, e in low:
                print('  0x%05X: %dx%d, H %s kHz, V %s Hz' % (o, e[1], e[2], khz(e[3]), khz(e[4])))
        else:
            print('WARNING: no 15 kHz entries in the table. Lowering the limit may not be enough.')

        if a.wide400 or a.smooth400:
            # (sampled width, H total) per variant; the original entry is 720 wide with H total 900
            variants = {'--wide400': (1280, 1600), '--smooth400': (1440, 1800)}
            opt = '--wide400' if a.wide400 else '--smooth400'
            width, htotal = variants[opt]
            known = {(720, 900)} | set(variants.values())
            text = [(o, e) for o, e in entries if e[1] in (720, 1280, 1440) and e[2:5] == (400, 315, 700)]
            gfx = [(o, e) for o, e in entries if e[1:5] == (640, 400, 315, 700)]
            if not text:
                sys.exit('%s: no 720x400 70 Hz entry found, nothing written.' % opt)
            o, e = text[0]  # (?) assumes the firmware uses the first matching entry
            if (e[1], e[7]) == (width, htotal):
                print('%s: already %d wide.' % (opt, width))
            elif (e[1], e[7]) not in known:
                sys.exit('%s: unexpected %d wide / H total %d in the 720x400 entry, nothing written.' % (opt, e[1], e[7]))
            else:
                # H start scaled from the 640x400 70 Hz entry (H total 800), same timing in samples
                hstart = int(gfx[0][1][9] * htotal / 800 + 0.5) if gfx else int(145 * htotal / 800 + 0.5)
                new = bytearray(d[o:o + ENTRY.size])
                struct.pack_into('>H', new, 1, width)
                struct.pack_into('>H', new, 11, htotal)
                struct.pack_into('>H', new, 15, hstart)
                for i in range(ENTRY.size):
                    if new[i] != d[o + i]:
                        changes.append((o + i, new[i], None))
                changes.append((o, d[o], '720x400 70 Hz entry at 0x%05X -> sampled %d wide (H total %d, H start %d)'
                                % (o, width, htotal, hstart)))

    # --- result ---
    real = [(p, v) for p, v, _ in changes if d[p] != v]
    print('\nChanges:')
    for _, _, desc in changes:
        if desc:
            print('  ' + desc)
    if not real:
        print('  none needed, the limits are already low.')
        return

    if not a.out:
        print('\n%d bytes would change. Rerun with -o patched.bin to write the copy.' % len(real))
        return
    for p, v in real:
        d[p] = v
    open(a.out, 'wb').write(d)
    print('\nWrote %s (%d bytes, %d bytes changed).' % (a.out, len(d), len(real)))
    print('Flash it, read it back and compare before trusting it.')


if __name__ == '__main__':
    main()
