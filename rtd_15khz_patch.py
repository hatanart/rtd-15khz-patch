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
  8. Optional (--ddc-debug, HP E190i and Fujitsu B19-7): adds DDC/CI code 0xE0 for reading the
     scaler's registers live over the VGA or DVI cable with ddc_probe.py (Fujitsu: also 0xE1 to write).
  9. Optional (--aspect43, HP E190i, ThinkVision LT1913p and Fujitsu B19-7): shows modes below 960 lines as 4:3 (1280x960 with
     black bars) instead of stretched to the 5:4 panel. Native 1280x1024 is unchanged.
 10. Optional (--manual-auto, HP E190i and Fujitsu B19-7): VGA auto adjust runs only when you ask for it
     (menu or DDC/CI), not by itself whenever a new mode appears.
 11. Optional (--ega350, HP E190i and ThinkVision LT1913p): shows 350-line modes (EGA graphics
     and text at 70 Hz) at full height instead of inside a 400-line window with black bands.
 12. Generic RTD2660 firmware (Realtek's stock code, as on many cheap driver boards): its limits
     are already low, but VGA rejects 15 kHz with "Mode not support". Its only 15 kHz entry has the
     wrong line count (525 instead of 262 per field) and the fallback refuses anything below 420
     lines. Fixed automatically: that entry becomes 240p NTSC, and the 1080i entry after it (33.7 kHz,
     not used over VGA) becomes 288p PAL. --smooth400 also works there and makes DOS text mode clean;
     320x200 games stay uneven on a 1024 wide panel (3.2 panel pixels per game pixel).

It refuses to write anything if the patterns are missing or look unusual.

Tested only on: HP EliteDisplay E190i (patched; 15 kHz works on VGA and DisplayPort with no
warning box, menus and auto adjust work; HDMI sources work on DVI; --wide400, --smooth400, --ddc-debug, --aspect43, --manual-auto and --ega350 work)
and Lenovo ThinkVision LT1913p (already has the low limits, nothing to patch), an RTD2660H
driver board (PCB800099, stock firmware: Amiga on VGA works after the mode table fix), and a
Fujitsu B19-7 LED (no warning box; HDMI sources on DVI, --smooth400, --manual-auto,
--no-input-box, --aspect43 (VGA DOS modes) and --ddc-debug work).

Usage:
  python3 rtd_15khz_patch.py dump.bin                 # analyse only
  python3 rtd_15khz_patch.py dump.bin -o patched.bin  # write patched copy
  python3 rtd_15khz_patch.py dump.bin -o patched.bin --wide400     # or --smooth400
  python3 rtd_15khz_patch.py dump.bin --trim -o patched.bin   # fix a wrapped read
  python3 rtd_15khz_patch.py dump.bin -o patched.bin --ddc-debug  # HP E190i: live debug code
  python3 rtd_15khz_patch.py dump.bin -o patched.bin --aspect43   # HP E190i: 4:3 instead of 5:4
  python3 rtd_15khz_patch.py dump.bin -o patched.bin --manual-auto  # HP E190i: no automatic auto adjust
  python3 rtd_15khz_patch.py dump.bin -o patched.bin --ega350       # 350-line modes at full height

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

# The same warning in a simpler form (Fujitsu B19-7, bank 2 at 0x9547): the flag is set when the
# mode is wider than 1280 or taller than 1024. The 15 kHz table entries are 1440 wide (sampled at
# twice the rate), so every 240p/288p signal got "Frequency out of range ... change to 1280 x 1024".
#   MOV DPTR,#flags ; MOVX A,@DPTR ; ANL A,#~bit ; MOVX @DPTR,A      <- clears the warning flag
#   SETB C ; width - 0x0500 - C ; JNC set (wider than 1280) ; height - 0x0400 - C ; JC done (up to 1024)
#   set: MOV DPTR,#flags ; MOVX A,@DPTR ; ORL A,#bit ; MOVX @DPTR,A ; done: CLR C ; RET
# The fix returns (CLR C ; RET) right after the flag is cleared.
SIZE_WARN = re.compile(
    rb'\x90(..)\xE0\x54\xFB\xF0(\xD3\xE5|\xC3\x22).\x94\x00\xE5.\x94\x05\x50\x0A'
    rb'\xE5.\x94\x00\xE5.\x94\x04\x40\x07\x90\1\xE0\x44\x04\xF0\xC3\x22', re.S)

# --no-input-box (Fujitsu B19-7): after every mode change the firmware posts an OSD event:
#   warning flag set -> 0x17 (out of range box); VGA mode needing auto -> 0x11; else -> 0x12,
#   the "Input: VGA" / "Input: DVI" box, unless a setting bit is set:
#   MOV DPTR,#ev ; MOV A,#11h ; MOVX ; SJMP done ;
#   MOV DPTR,#cfg ; MOVX A,@DPTR ; MOV R7,A ; RRC x3 ; ANL A,#1Fh ; JB ACC.0,done ;
#   MOV DPTR,#ev ; MOV A,#12h ; MOVX
# The fix turns JB ACC.0,+6 into SJMP +7 (same target), so the 0x12 event is never posted.
INPUT_BOX = re.compile(rb'\x90(..)\x74\x11\xF0\x80\x13\x90..\xE0\xFF\x13\x13\x13\x54\x1F'
                       rb'(\x20\xE0\x06|\x80\x07\x06)\x90\1\x74\x12\xF0', re.S)


def no_input_box_changes(d):
    m = list(INPUT_BOX.finditer(d))
    if len(m) != 1:
        sys.exit('--no-input-box: input box event matched %d times (expected 1), nothing written.' % len(m))
    pos = m[0].start(2)
    if d[pos] == 0x80:
        print('--no-input-box: already present.')
        return []
    return [(pos, 0x80, 'no "Input: VGA/DVI" box after mode changes, at 0x%05X' % pos), (pos + 1, 0x07, None)]


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


# Realtek's stock RTD2660 mode table: "1440 x 480i" (V total 525, but lines are counted per field,
# so it never matches) followed by "1920 x 1080i" (562 = 1125 / 2).
RTD2660_480I = ENTRY.pack(0x0D, 1440, 240, 157, 600, 10, 12, 1716, 525, 276, 59)
RTD2660_1080I = ENTRY.pack(0x0D, 1920, 540, 337, 600, 10, 12, 2200, 562, 236, 33)
RTD2660_NTSC = ENTRY.pack(0x0F, 1440, 240, 157, 600, 10, 12, 1716, 262, 276, 18)
RTD2660_PAL = ENTRY.pack(0x0F, 1440, 288, 156, 500, 10, 12, 1728, 312, 276, 22)
# its first 31.5 kHz / 70 Hz 400-line entry for H- V+ (DOS text and 320x200), sampled 640 of 900
RTD2660_400 = ENTRY.pack(0x0C, 640, 400, 315, 700, 10, 12, 900, 449, 162, 37)
RTD2660_400S = ENTRY.pack(0x0C, 1440, 400, 315, 700, 10, 12, 1800, 449, 324, 37)


def rtd2660_smooth400_changes(d):
    n_old, n_new = d.count(RTD2660_400), d.count(RTD2660_400S)
    if n_old + n_new != 1:
        sys.exit('--smooth400: RTD2660 400-line entry matched %d times (expected 1), nothing written.' % (n_old + n_new))
    if n_new:
        print('--smooth400: already 1440 wide.')
        return []
    o = d.find(RTD2660_400)
    out = [(o + i, RTD2660_400S[i], None) for i in range(ENTRY.size) if RTD2660_400S[i] != d[o + i]]
    out.append((o, d[o], 'RTD2660 640x400 70 Hz entry at 0x%05X -> sampled 1440 wide (H total 900 -> 1800, H start 324)' % o))
    return out


def rtd2660_changes(d):
    """None if this is not the stock RTD2660 table, else the changes (empty if already done)."""
    old, new = RTD2660_480I + RTD2660_1080I, RTD2660_NTSC + RTD2660_PAL
    n_old, n_new = d.count(old), d.count(new)
    if n_old + n_new == 0:
        return None
    if n_old + n_new > 1:
        sys.exit('RTD2660 mode table entries matched %d times (expected 1), nothing written.' % (n_old + n_new))
    if n_new:
        print('RTD2660 15 kHz VGA entries: already present.')
        return []
    o = d.find(old)
    out = [(o + i, new[i], None) for i in range(len(new)) if new[i] != d[o + i]]
    out.append((o, d[o], 'RTD2660 stock table: 1440x480i entry at 0x%05X -> 240p NTSC (V total 525 -> 262)' % o))
    out.append((o + ENTRY.size, d[o + ENTRY.size],
                'RTD2660 stock table: 1080i entry at 0x%05X -> 288p PAL (15.6 kHz / 50 Hz, V total 312)' % (o + ENTRY.size)))
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


# --manual-auto (HP E190i, Fujitsu B19-7): when a VGA mode is ready, the firmware asks "has this mode never
# been auto adjusted?" and, if so, queues the auto adjust event (0x0B). The flag is set for every new
# mode slot and cleared only by a successful auto adjust, so a failed run repeats at every resync.
#   LCALL is_vga ; MOV A,R7 ; JNZ skip ; LCALL needs_auto ; JNC skip ; MOV DPTR,#event ; MOV A,#0Bh ; MOVX
# The fix replaces LCALL needs_auto with CLR C ; NOP ; NOP. The menu item and DDC/CI VCP 0x1E
# (auto setup) call the auto adjust routine directly and keep working.
# The Fujitsu B19-7 has the same branch with its own event code (0x11), one byte shorter.
AUTO_TRIGGER = re.compile(rb'\x12..\xEF\x70\x0E(\x12..|\xC3\x00\x00)\x50\x09\x90..\x74\x0B\xF0', re.S)
AUTO_TRIGGER_FJ = re.compile(rb'\x12..\xEF\x70\x0D(\x12..|\xC3\x00\x00)\x50\x08\x90..\x74\x11\xF0', re.S)


def manual_auto_changes(d):
    m = list(AUTO_TRIGGER.finditer(d)) + list(AUTO_TRIGGER_FJ.finditer(d))
    if len(m) != 1:
        sys.exit('--manual-auto: auto adjust trigger matched %d times (expected 1), nothing written.' % len(m))
    pos = m[0].start(1)
    if d[pos] == 0xC3:
        print('--manual-auto: already present.')
        return []
    return [(pos, 0xC3, 'auto adjust only on request (menu / DDC/CI), not on new modes, at 0x%05X' % pos),
            (pos + 1, 0x00, None), (pos + 2, 0x00, None)]


# --ega350: after a VGA mode is set up, the firmware turns any 640 or 720 wide, 350-line input
# into 400 lines and moves its start 25 lines up, so EGA modes sit inside a 400-line window:
#   MOV A,width_lo ; CJNE A,#80h,next ; MOV A,width_hi ; CJNE A,#02h,next ;
#   MOV A,height_lo ; CJNE A,#5Eh,next ; MOV A,height_hi ; CJNE A,#01h,next ; MOV height,#0190h ...
# (then the same for 720). The fix makes the routine return at once. The HP E190i also has no
# 640x350 entry for the 350-line sync polarity (H+ V-) but a 640x410 one, which is changed to
# 640x350 with the start 30 lines later (the ThinkVision already has 640x350, start 61).
EGA_OVERRIDE = re.compile(rb'(\xE5|\x22).\xB4\x80\x21\xE5.\xB4\x02\x1C\xE5.\xB4\x5E\x17\xE5.\xB4\x01\x12'
                          rb'\x75.\x01\x75.\x90', re.S)
SYNC_HP_VN = 0x02   # mode table polarity flag of the VGA 350-line modes


def ega350_changes(d, entries):
    m = list(EGA_OVERRIDE.finditer(d))
    if len(m) != 1:
        sys.exit('--ega350: 350-to-400 line routine matched %d times (expected 1), nothing written.' % len(m))
    out = []
    pos = m[0].start()
    if d[pos] == 0x22:
        print('--ega350: 350-line routine already disabled.')
    else:
        out.append((pos, 0x22, 'keep 350-line modes at 350 lines (routine at 0x%05X returns at once)' % pos))
    ega = [(o, e) for o, e in entries if e[0] & 0x0F == SYNC_HP_VN and e[1] == 640 and e[3:5] == (315, 700)
           and e[8] == 449]
    if not ega:
        sys.exit('--ega350: no 640 wide 70 Hz entry for the 350-line sync polarity, nothing written.')
    o, e = ega[0]
    if e[2] == 410:
        new = bytearray(d[o:o + ENTRY.size])
        struct.pack_into('>H', new, 3, 350)
        struct.pack_into('>H', new, 17, e[10] + 30)
        out += [(o + i, new[i], None) for i in range(ENTRY.size) if new[i] != d[o + i]]
        out.append((o, d[o], '640x410 70 Hz entry at 0x%05X -> 640x350 (V start %d -> %d)' % (o, e[10], e[10] + 30)))
    elif e[2] != 350:
        sys.exit('--ega350: unexpected %dx%d in the 350-line entry at 0x%05X, nothing written.' % (e[1], e[2], o))
    return out


# --ddc-debug, Fujitsu B19-7 layout: the DDC/CI "Get VCP" handler builds its reply at F9B9 and
# switches over the VCP code (F994) with a ?C?CCASE table; "Set VCP" (value in F995/F996) has
# its own table. Both defaults (unsupported code) are redirected to a stub in the same bank:
#   get E0: reply = byte at the stored address (max = the address);  set E0: store the address;
#   set E1: write the low byte of the value to the stored address (ddc_probe.py poke/disp/ports).
FJ_GET = re.compile(rb'\x90\xF9\xB9\x74\x6E\xF0\xA3\x74\x88\xF0\xA3\x74\x02\x12..\x12..', re.S)
FJ_SET = re.compile(rb'\x90\xF9\x94\xE0\xFF\x64\x02\x60\x0C\xEF\x64\x04\x60\x07\x90..\xEF\xF0\xC2.\xEF\x12..', re.S)


def ccase_default(d, j):
    """Position of the default address after a ?C?CCASE table starting at j."""
    while not (d[j] == 0 and d[j + 1] == 0):
        j += 3
        if j > len(d) - 3:
            return None
    return j + 2


def ddc_debug_fujitsu(d):
    g, st = list(FJ_GET.finditer(d)), list(FJ_SET.finditer(d))
    if len(g) != 1 or len(st) != 1 or g[0].start() >> 16 != st[0].start() >> 16:
        return None
    bank = g[0].start() & ~0xFFFF
    gdef, sdef = ccase_default(d, g[0].end()), ccase_default(d, st[0].end())
    get_def, set_def = d[gdef] << 8 | d[gdef + 1], d[sdef] << 8 | d[sdef + 1]
    stub, mem = 0xF000, 0xFEFA
    get_done = get_def - 5            # the common "send reply" path just before the default
    if get_def == stub:
        print('--ddc-debug: already present (Fujitsu B19-7 layout).')
        return []
    expect = {get_done: '12b4bb8006', get_def: '90f9bc7401f0'}
    for addr, hexs in expect.items():
        if d[bank + addr:bank + addr + len(hexs) // 2] != bytes.fromhex(hexs):
            sys.exit('--ddc-debug: code at %X:%04X is not what the Fujitsu B19-7 has, nothing written.' % (bank >> 16, addr))
    if any(x != 0xFF for x in d[bank + stub - 16:bank + stub + 0x80]):
        sys.exit('--ddc-debug: no free space at %X:%04X, nothing written.' % (bank >> 16, stub))
    if re.search(bytes([0x90, mem >> 8]) + b'[\xFA\xFB]', d):
        sys.exit('--ddc-debug: RAM at 0x%04X is used by the firmware, nothing written.' % mem)
    w = lambda a: bytes([a >> 8, a & 0xFF])
    m = w(mem)
    get = (bytes.fromhex('90f994e0' 'b4e002' '8003') + b'\x02' + w(get_def) +
           b'\x90' + m + bytes.fromhex('e0fca3e0fd' '8c838d82' 'e0fb7a007f00') + b'\x02' + w(get_done))
    e0 = bytes.fromhex('90f995e0fea3e0ff') + b'\x90' + m + bytes.fromhex('eef0a3eff0' '22')
    setp = (bytes.fromhex('90f994e0') + bytes([0xB4, 0xE0, len(e0)]) + e0 +
            bytes.fromhex('b4e102' '8003') + b'\x02' + w(set_def) +
            bytes.fromhex('90f996e0ff') + b'\x90' + m + bytes.fromhex('e0fca3e0fd' '8c838d82' 'eff0' '22'))
    code = get + setp
    out = [(bank + stub + i, b, None) for i, b in enumerate(code)]
    out += [(gdef, stub >> 8, 'DDC/CI debug VCP 0xE0/0xE1 (Fujitsu B19-7 layout, code at %X:%04X, address kept at 0x%04X)'
             % (bank >> 16, stub, mem)), (gdef + 1, stub & 0xFF, None),
            (sdef, (stub + len(get)) >> 8, None), (sdef + 1, (stub + len(get)) & 0xFF, None)]
    return out


def ddc_debug_changes(d):
    """Add DDC/CI VCP code 0xE0 for live debugging (see ddc_probe.py peek/watch):
    'set E0 = address' remembers an address, 'get E0' returns the byte at that address
    (current value) and the address itself (maximum value). Only tested on the HP E190i, so
    every routine the new code jumps to is checked byte for byte first."""
    m = list(VCP_TABLE.finditer(d))
    if len(m) != 1:
        fj = ddc_debug_fujitsu(d)
        if fj is not None:
            return fj
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


# --aspect43: bank 0 layouts of the tested firmwares. Both are built from the same Realtek code
# (compare the RTD2660 reference: stModeInfo / stDisplayInfo / CAdjustIVS2DVSDelay), with the
# variables one byte apart. Every byte the patch relies on is checked before anything is written.
A43_LAYOUTS = [
    dict(name='HP E190i', var=0x00,       # IDATA addresses below are for var = 0 (HP)
         setup=0xDC80, setup_bytes='12d91812e63a12e5ca', defaults_fn=0xE63A, defaults=0xD58D, tail=0xB994,
         start_at=0x59F5, delay=0x1950, bg_at=0x2BBE, lmul=0xB03F),
    dict(name='ThinkVision LT1913p', var=-1,
         setup=0xBDFF, setup_bytes='12bafc12c6d312c669', defaults_fn=0xC6D3, defaults=0xB750, tail=0xD1DE,
         start_at=0x5A27, delay=0x10C6, bg_at=0x2694, lmul=0xCD10),
    # top=True: the picture is not moved down; all 64 black lines go below it. On the Fujitsu,
    # picture lines drawn below panel row 966 (the unshifted picture end) break up (doubled,
    # sparkling), although every display window, frame and delay register reads back correctly.
    # capture: 4:3 by capturing blank lines instead of a smaller output window; hooked where the
    # firmware itself grows the capture (0:A1D6: LCALL 0:B1EB, just before the capture registers
    # are programmed by 0:6947).
    # input_type: R7 = 0 for VGA (IDATA 0x9E based; the auto adjust code uses it too). Capturing
    # blanking lines only works on VGA; digital inputs (DVI) get the picture-at-the-top method.
    dict(name='Fujitsu B19-7', var=0x00, capture=True, cap_hook=0xA1D6, cap_next=0xB1EB, input_type=0xC3E1,
         setup=0xA495, setup_bytes='12a1b712c4ad12c44b', defaults_fn=0xC4AD, defaults=0x9E82, tail=0xB1F0,
         start_at=0x4BBE, delay=0x106C, bg_at=0x22D5, lmul=0xAD11),
]
A43_CODE = 0xF000
CAP_MARGIN = 26   # Fujitsu: lines that must stay outside the capture (its own routine at 2:8001 uses 26)
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
    extra = ({L['cap_hook']: b'\x12' + w(L['cap_next']),
              L['input_type']: bytes.fromhex('789ee612725b5407ff22'), 0x725B: bytes.fromhex('25e02497f8e622')}
             if L.get('capture') else {})
    return {
        **extra,
        L['setup']: bytes.fromhex(L['setup_bytes']),
        L['defaults_fn']: j(0x12, L['defaults']) + j(0x02, L['tail']),
        L['defaults'] + 0x18: bytes([0x75, dv, 4, 0x75, dv + 1, 0, 0x75, pv, 4, 0x75, pv + 1, 0]),
        L['start_at']: bytes([0x50, 0x06, 0x75, ds, 0, 0x75, ds + 1, 6]) + j(0x12, L['delay']),
        L['bg_at']: bg,
        L['lmul']: bytes.fromhex('e88ff0a4cc8bf0a4'),
        L['delay']: bytes([0xAE, v(0x3B), 0xAF, v(0x3C)]),
    }


def asm51(items, base):
    """Tiny assembler: items are byte values, ('L', name) labels and ('S', name) for an 8-bit
    relative jump offset to a label. Returns the bytes; every offset is range checked."""
    pos, labels = 0, {}
    for it in items:
        if isinstance(it, tuple) and it[0] == 'L':
            labels[it[1]] = pos
        else:
            pos += 1
    out, pos = bytearray(), 0
    for it in items:
        if isinstance(it, tuple) and it[0] == 'L':
            continue
        if isinstance(it, tuple):
            rel = labels[it[1]] - (pos + 1)
            assert -0x80 <= rel < 0x80, (it, rel)
            out.append(rel & 0xFF)
        else:
            out.append(it)
        pos += 1
    return bytes(out), {k: base + v for k, v in labels.items()}


def aspect43_capture_code(L):
    """Fujitsu B19-7: 4:3 only where the input has room for it.
    Only on VGA: if the input has enough blanking lines (room = IVTotal - CAP_MARGIN - IVHeight is at least
    half of extra = about IVHeight / 15), the picture keeps the full panel height and the scaler
    captures up to 'extra' more lines, half above and half below (CAP, hooked at cap_hook where the
    firmware itself grows the capture; 26 lines is the firmware's own margin, see 2:8001). The
    blanking lines become the bars and the picture is centred.
    Otherwise (Amiga 288/312 and 240/262 leave no room, DVI has no blanking to capture) the mode
    stays full screen: the HP method (smaller output window) does not work here, because on this
    monitor anything but black drawn below panel row 966 breaks up once the output height is 960.
    Needs IVHeight < 960 and IVTotal * 960 >= A43_VMIN * IVHeight."""
    v = lambda x: x + L['var']
    w = lambda a: bytes([a >> 8, a & 0xFF])
    dv, ivt, ivh, ivs = v(0x45), v(0x33), v(0x35), v(0x37)
    lm = list(b'\x12' + w(L['lmul']))
    h = lambda x: list(bytes.fromhex(x))

    def checks(done):   # common conditions; jump to 'done' when 4:3 does not apply
        return ([0xC3, 0xE5, ivh + 1, 0x94, 0xC0, 0xE5, ivh, 0x94, 0x03, 0x50, ('S', done),     # IVHeight >= 960
                 0xE5, ivh, 0x45, ivh + 1, 0x60, ('S', done)] +                                # IVHeight == 0
                [0x7C, 0, 0x7D, 0, 0xAE, ivt, 0xAF, ivt + 1, 0x78, 0, 0x79, 0, 0x7A, 0x03, 0x7B, 0xC0] + lm +
                h('c004c005c006c007') +
                [0x7C, 0, 0x7D, 0, 0xAE, ivh, 0xAF, ivh + 1,
                 0x78, 0, 0x79, 0, 0x7A, A43_VMIN >> 8, 0x7B, A43_VMIN & 0xFF] + lm +
                h('d003d002d001d000' 'c3eb9fea9ee99de89c') + [0x40, ('S', done)])            # too little blanking

    def room(no):       # R2 = extra, R4:R5 = room; jump to 'no' when capturing does not apply
        return ([0x7C, 0, 0x7D, 0, 0xAE, ivh, 0xAF, ivh + 1, 0x78, 0, 0x79, 0, 0x7A, 0x08, 0x7B, 0x89] + lm +
                h('ef24ffff' 'ee347ffe' 'ed3400fd' 'ee33' 'ed33' 'fa') +                       # R2 = extra
                [0xC3, 0xE5, ivt + 1, 0x95, ivh + 1, 0xFD, 0xE5, ivt, 0x95, ivh, 0xFC,           # R4:R5 = total - height
                 0xC3, 0xED, 0x94, CAP_MARGIN, 0xFD, 0xEC, 0x94, 0x00, 0xFC, 0x40, ('S', no),    # - margin < 0: no
                 0xEC, 0x70, 0x08, 0xEA, 0xC3, 0x13, 0xFB, 0xC3, 0xED, 0x9B, 0x40, ('S', no)])   # room < extra/2: no

    # S1 (mode setup, after the panel defaults): output height 960 only where capturing does not apply
    vga = lambda no: [0x12] + list(w(L['input_type'])) + [0xEF, 0x70, ('S', no)]     # not VGA: no
    s1 = ([0x12] + list(w(L['defaults'])) + checks('x') + vga('win') + room('win') +
          [('L', 'x'), 0x02] + list(w(L['tail'])) +
          [('L', 'win'), 0x75, dv, 0x03, 0x75, dv + 1, 0xC0, 0x02] + list(w(L['tail'])))
    # CAP (before the capture registers are written): grow the capture where it applies
    cap = (checks('x') + vga('x') + room('x') +
           h('ec7007c3ed9a5002edfa') +                                                       # extra = min(extra, room)
           [0xE5, ivh + 1, 0x2A, 0xF5, ivh + 1, 0xE5, ivh, 0x34, 0x00, 0xF5, ivh] +            # IVHeight += extra
           h('eac313f9') +                                                                     # R1 = extra / 2
           [0xC3, 0xE5, ivs + 1, 0x99, 0xFB, 0xE5, ivs, 0x94, 0x00, 0xFA, 0x40, ('S', 'x'),    # R2:R3 = IVStart - R1
            0xC3, 0xEB, 0x94, 0x06, 0xEA, 0x94, 0x00, 0x40, ('S', 'x'),                        # below 6: keep
            0x8A, ivs, 0x8B, ivs + 1,                                                          # IVStart = R2:R3
            ('L', 'x'), 0x02] + list(w(L['cap_next'])))
    # the checks and the room test are long: the early jumps get a second exit in reach.
    # Only CAP is used: the picture-at-the-top fallback (s1 with S2/S3) was dropped, modes
    # without room (Amiga, DVI) stay full screen. s1 is kept above for reference.
    del s1
    code_cap, _ = asm51(split_exit(cap, L['cap_next']), A43_CODE)
    return code_cap, [(L['cap_hook'], b'\x12' + w(A43_CODE))]


def split_exit(items, target):
    """Insert 'SJMP over; x2: LJMP target' after the frame check and send the early 'x' jumps
    there, so every relative jump stays in range."""
    # the extra exit goes right after the third 'x' jump (the end of the common checks)
    n, cut = 0, None
    for i, it in enumerate(items):
        if it == ('S', 'x'):
            n += 1
            if n == 3:
                cut = i + 1
                break
    out = []
    for i, it in enumerate(items[:cut]):
        out.append(('S', 'x2') if it == ('S', 'x') else it)
    out += [0x80, 0x03, ('L', 'x2'), 0x02, target >> 8, target & 0xFF]
    return out + items[cut:]


def aspect43_code(L, s23_base=None):
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
    if L.get('top'):
        s2 = s2[:12] + b'\x80' + s2[13:]                          # off == 0: JZ -> SJMP, keep DV start
        i = s3.index(bytes.fromhex('c313fced13fd'))
        s3 = s3[:i] + bytes.fromhex('e4fcfd000000') + s3[i + 6:]  # background starts at DV start
    assert len(s1) == 0x5E and len(s2) == 0x2D
    if s23_base is not None:                  # only S2 + S3, for the Fujitsu's combined code
        return None, (s2 + s3, None)
    p1, p2, p3 = A43_CODE, A43_CODE + len(s1), A43_CODE + len(s1) + len(s2)
    hooks = [(L['setup'] + 3, b'\x12' + w(p1)), (L['start_at'] + 8, b'\x12' + w(p2)), (L['bg_at'], b'\x02' + w(p3))]
    return s1 + s2 + s3, hooks


def aspect43_changes(d):
    """Show every mode with fewer than 960 lines as 4:3 (1280x960, 32 black lines top and
    bottom) instead of stretched to the 5:4 panel. Modes whose timing leaves too little
    vertical blanking for a 960-line picture (800x600@60, 1024x768) stay as they are."""
    for L in A43_LAYOUTS:
        code, hooks = aspect43_capture_code(L) if L.get('capture') else aspect43_code(L)
        if bytes(d[A43_CODE:A43_CODE + len(code)]) == code and all(d[p:p + 3] == h for p, h in hooks):
            print('--aspect43: already present (%s layout).' % L['name'])
            return []
        if all(d[a:a + len(b)] == b for a, b in a43_layout_bytes(L).items()):
            break
    else:
        sys.exit('--aspect43: the display code is not what the HP E190i, ThinkVision LT1913p or Fujitsu B19-7 has, nothing written.')
    if any(x != 0xFF for x in d[A43_CODE - 16:A43_CODE + max(0x100, len(code) + 16)]):
        sys.exit('--aspect43: no free space at 0:%04X, nothing written.' % A43_CODE)
    out = [(A43_CODE + i, b, None) for i, b in enumerate(code)]
    for p, h in hooks:
        out += [(p + i, b, None) for i, b in enumerate(h)]
    what = ('4:3 picture for VGA modes with enough blanking lines (captured as the bars), others stay full screen'
            if L.get('capture') else '4:3 picture (1280x960) for modes below 960 lines')
    out[0] = (A43_CODE, code[0], '%s, %s layout (code at 0:%04X)' % (what, L['name'], A43_CODE))
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
                    help='HP E190i / ThinkVision LT1913p / Fujitsu B19-7: show modes below 960 lines as 4:3 (1280x960 letterbox) instead of 5:4')
    ap.add_argument('--manual-auto', action='store_true',
                    help='HP E190i / Fujitsu B19-7: run VGA auto adjust only from the menu or button, never by itself on new modes')
    ap.add_argument('--ega350', action='store_true',
                    help='show 350-line modes (EGA, 70 Hz) at full height instead of inside a 400-line window')
    ap.add_argument('--no-input-box', action='store_true',
                    help='Fujitsu B19-7: no "Input: VGA/DVI" box every time the source changes mode')
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
        # Realtek's stock RTD2660 code keeps its limits in the panel data (already 10 kHz / 49 Hz);
        # there only the VGA mode table needs fixing
        rtd = rtd2660_changes(d)
        if rtd is None:
            sys.exit('No matching range check. This firmware is different, nothing written.')
        print('Stock RTD2660 firmware: the limits are in the panel data, only the VGA mode table needs a fix.')
        if a.ddc_debug or a.aspect43 or a.manual_auto or a.ega350 or a.wide400 or a.no_input_box:
            sys.exit('Of the optional patches, only --smooth400 applies to RTD2660 firmware. Nothing written.')
        if a.smooth400:
            rtd += rtd2660_smooth400_changes(d)
        return write_result(a, d, rtd)
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
    size_warn = list(SIZE_WARN.finditer(d))
    if len(size_warn) > 1 or (size_warn and warn):
        sys.exit('Out-of-range warning patterns matched %d / %d times. Not safe to patch, nothing written.'
                 % (len(warn), len(size_warn)))
    if size_warn:
        spos = size_warn[0].start(2)
        if d[spos] == 0xC3:
            print('\nOut-of-range warning (size check) at 0x%05X: already disabled.' % spos)
        else:
            print('\nOut-of-range warning (size check) at 0x%05X.' % spos)
            changes.append((spos, 0xC3, 'disable the "Frequency out of range ... 1280 x 1024" box (size check) at 0x%05X' % spos))
            changes.append((spos + 1, 0x22, None))
    elif not warn:
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
    if a.no_input_box:
        changes += no_input_box_changes(d)

    # --- mode table ---
    start, entries = find_mode_table(d)
    if start is None:
        print('\nVGA mode table: not found.')
        if a.ega350:
            sys.exit('--ega350: needs the VGA mode table, nothing written.')
    else:
        print('\nVGA mode table at 0x%05X, %d entries.' % (start, len(entries)))
        low = [(o, e) for o, e in entries if e[3] < 200]
        if low:
            print('15 kHz entries:')
            for o, e in low:
                print('  0x%05X: %dx%d, H %s kHz, V %s Hz' % (o, e[1], e[2], khz(e[3]), khz(e[4])))
        else:
            print('WARNING: no 15 kHz entries in the table. Lowering the limit may not be enough.')

        if a.ega350:
            changes += ega350_changes(d, entries)

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

    write_result(a, d, changes)


def write_result(a, d, changes):
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
