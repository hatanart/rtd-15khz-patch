#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""
ddc_probe.py - talk to a Realtek scaler over the monitor's DDC (I2C) lines, using
only Python 3 and the Linux kernel's /dev/i2c-* devices (no i2c-tools needed).

Usage (as root, after "modprobe i2c-dev"):
  python3 ddc_probe.py list                 # list I2C buses and their names
  python3 ddc_probe.py scan BUS             # which addresses answer on that bus
  python3 ddc_probe.py get BUS REG [REG...] # read scaler ISP registers (slave 0x4A)
  python3 ddc_probe.py set BUS REG VALUE    # write one ISP register (only "safe" ones)
  python3 ddc_probe.py test BUS             # mailbox test: write/read back 0x64-0x66, 0x72-0x74
  python3 ddc_probe.py poll BUS REG [REG...]  # print the registers twice a second, Ctrl-C to stop
  python3 ddc_probe.py vcp BUS CODE [CODE...] # DDC/CI "get VCP feature" (e.g. 0x10 = brightness)
  python3 ddc_probe.py vcpset BUS CODE VALUE  # DDC/CI "set VCP feature" (0x10, or debug codes 0xE0-0xE2)

With the instrumented firmware (debug VCP 0xE0 added), read the scaler's own memory
and registers live while the monitor runs normally:
  python3 ddc_probe.py peek BUS ADDR [ADDR...]   # read bytes, e.g. peek 1 0xFEDD 0x02A4
  python3 ddc_probe.py watch BUS ADDR [ADDR...]  # keep reading, print a line whenever a value changes
  python3 ddc_probe.py dump BUS START END FILE    # save firmware RAM (0xF800-0xFEFF) to a file
  python3 ddc_probe.py diff FILE_A FILE_B        # show which RAM bytes differ between two dumps
  python3 ddc_probe.py poke BUS ADDR VALUE       # write one byte (needs debug VCP 0xE1: Fujitsu
                                                 # B19-7 with --ddc-debug; only the frame sync delay registers
                                                 # 0x40-0x44 are allowed)
  python3 ddc_probe.py disp BUS                  # read the display timing registers (DH/DV total,
                                                 # background/active start and end) through the
                                                 # 0x2A/0x2B port; needs VCP 0xE1 (Fujitsu)
  python3 ddc_probe.py dispset BUS NAME VALUE    # live test: set 'vact' (V active start, end follows)
                                                 # or 'vbg' (V background start, end follows);
                                                 # undone by any mode change
  python3 ddc_probe.py ports BUS                 # read the display format (0x2A/2B), highlight window
                                                 # (0x60/61) and scale-up (0x33/34) registers; same build
Only read addresses you were told to: some scaler registers are data ports where a
read moves an internal pointer.

Numbers can be given as 0x.. or decimal. Only slave 0x4A is ever read or written.
Writes are limited to 0x64-0x66 and 0x72-0x74 (plain address registers of the
flash engine); 0x60 and 0x6F start flash operations / programming mode and are refused.
"""
import ctypes
import fcntl
import os
import re
import sys
import time

I2C_SLAVE = 0x0703
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001
RTD = 0x4A
SAFE_WRITE = (0x64, 0x65, 0x66, 0x72, 0x73, 0x74)


class Msg(ctypes.Structure):
    _fields_ = [('addr', ctypes.c_uint16), ('flags', ctypes.c_uint16),
                ('len', ctypes.c_uint16), ('buf', ctypes.POINTER(ctypes.c_uint8))]


class RdWr(ctypes.Structure):
    _fields_ = [('msgs', ctypes.POINTER(Msg)), ('nmsgs', ctypes.c_uint32)]


def num(s):
    try:
        return int(s, 0)
    except ValueError:
        sys.exit('"%s" is not a number. Use the bus number from "list" (e.g. 1 for i2c-1), '
                 'and registers/values as 0x.. or decimal.' % s)


def open_bus(bus):
    try:
        return os.open('/dev/i2c-%d' % bus, os.O_RDWR)
    except OSError as e:
        sys.exit('Cannot open /dev/i2c-%d: %s (run as root, and "modprobe i2c-dev" first)' % (bus, e))


def read_reg(fd, reg, addr=RTD):
    """Register read with a repeated start (like i2c_smbus_read_byte_data)."""
    wbuf = (ctypes.c_uint8 * 1)(reg)
    rbuf = (ctypes.c_uint8 * 1)()
    msgs = (Msg * 2)(Msg(addr, 0, 1, wbuf), Msg(addr, I2C_M_RD, 1, rbuf))
    fcntl.ioctl(fd, I2C_RDWR, RdWr(msgs, 2))
    return rbuf[0]


def write_reg(fd, reg, val, addr=RTD):
    buf = (ctypes.c_uint8 * 2)(reg, val)
    msgs = (Msg * 1)(Msg(addr, 0, 2, buf))
    fcntl.ioctl(fd, I2C_RDWR, RdWr(msgs, 1))


def cmd_list():
    # /sys/class/i2c-adapter is missing on some kernels (e.g. Raspberry Pi OS); /sys/class/i2c-dev
    # (with i2c-dev loaded) and /sys/bus/i2c/devices list the same buses
    for base in ('/sys/class/i2c-adapter', '/sys/class/i2c-dev', '/sys/bus/i2c/devices'):
        names = [n for n in os.listdir(base) if re.fullmatch(r'i2c-\d+', n)] if os.path.isdir(base) else []
        if names:
            break
    else:
        sys.exit('No I2C adapters found. Try: modprobe i2c-dev')
    for name in sorted(names, key=lambda n: int(n.split('-')[1])):
        try:
            with open(os.path.join(base, name, 'name')) as f:
                label = f.read().strip()
        except OSError:
            label = '?'
        print('%-8s %s' % (name, label))
    print('\nThe VGA port is usually named like "i915 gmbus vga", "...crt" or "...analog".')


def cmd_scan(bus):
    fd = open_bus(bus)
    found = []
    for a in range(0x08, 0x78):
        try:
            fcntl.ioctl(fd, I2C_SLAVE, a)
            os.read(fd, 1)
            found.append(a)
        except OSError:
            pass
    os.close(fd)
    print('Answering on bus %d: %s' % (bus, ' '.join('0x%02X' % a for a in found) or 'nothing'))
    if 0x50 in found:
        print('  0x50 = EDID (monitor is connected)')
    print('  0x4A = Realtek scaler ISP: %s' % ('FOUND' if RTD in found else 'not found'))


def cmd_get(bus, regs):
    fd = open_bus(bus)
    for r in regs:
        try:
            print('0x%02X = 0x%02X' % (r, read_reg(fd, r)))
        except OSError as e:
            print('0x%02X : read failed (%s)' % (r, e))
    os.close(fd)


def cmd_set(bus, reg, val):
    if reg not in SAFE_WRITE:
        sys.exit('Refusing to write 0x%02X. Allowed: %s' % (reg, ' '.join('0x%02X' % r for r in SAFE_WRITE)))
    fd = open_bus(bus)
    write_reg(fd, reg, val & 0xFF)
    print('wrote 0x%02X = 0x%02X, reads back 0x%02X' % (reg, val & 0xFF, read_reg(fd, reg)))
    os.close(fd)


def cmd_test(bus):
    fd = open_bus(bus)
    print('ISP control 0x6F = 0x%02X (bit 7 set would mean programming mode is on)' % read_reg(fd, 0x6F))
    ok = True
    for i, r in enumerate(SAFE_WRITE):
        before = read_reg(fd, r)
        pattern = (0x5A + i * 0x11) & 0xFF
        write_reg(fd, r, pattern)
        after = read_reg(fd, r)
        write_reg(fd, r, before)
        good = after == pattern
        ok = ok and good
        print('0x%02X: was 0x%02X, wrote 0x%02X, read 0x%02X  %s' % (r, before, pattern, after, 'OK' if good else 'NO'))
    print('ISP control 0x6F = 0x%02X' % read_reg(fd, 0x6F))
    os.close(fd)
    print('\nMailbox %s.' % ('WORKS' if ok else 'does not work'))
    print('Did the picture or OSD do anything while this ran? Please note it.')


DDCCI = 0x37


def vcp_get(fd, code):
    """DDC/CI 'Get VCP Feature'. Returns (result, type, max, current) or raises OSError/ValueError."""
    req = [0x51, 0x82, 0x01, code]
    chk = 0x6E
    for b in req:
        chk ^= b
    buf = (ctypes.c_uint8 * 5)(*(req + [chk]))
    fcntl.ioctl(fd, I2C_RDWR, RdWr((Msg * 1)(Msg(DDCCI, 0, 5, buf)), 1))
    time.sleep(0.05)
    rbuf = (ctypes.c_uint8 * 11)()
    fcntl.ioctl(fd, I2C_RDWR, RdWr((Msg * 1)(Msg(DDCCI, I2C_M_RD, 11, rbuf)), 1))
    r = list(rbuf)
    chk = 0x50
    for b in r[:10]:
        chk ^= b
    if r[0] != 0x6E or r[2] != 0x02 or r[4] != code:
        raise ValueError('unexpected reply ' + ' '.join('%02X' % b for b in r))
    if chk != r[10]:
        raise ValueError('bad checksum in reply ' + ' '.join('%02X' % b for b in r))
    return r[3], r[5], r[6] << 8 | r[7], r[8] << 8 | r[9]


def vcp_set(fd, code, value):
    req = [0x51, 0x84, 0x03, code, (value >> 8) & 0xFF, value & 0xFF]
    chk = 0x6E
    for b in req:
        chk ^= b
    buf = (ctypes.c_uint8 * 7)(*(req + [chk]))
    fcntl.ioctl(fd, I2C_RDWR, RdWr((Msg * 1)(Msg(DDCCI, 0, 7, buf)), 1))
    time.sleep(0.05)


def cmd_vcp(bus, codes):
    fd = open_bus(bus)
    for c in codes:
        try:
            res, typ, mx, cur = vcp_get(fd, c)
            print('VCP 0x%02X: %s, current %d (0x%04X), max %d' % (
                c, 'ok' if res == 0 else 'UNSUPPORTED', cur, cur, mx))
        except (OSError, ValueError) as e:
            print('VCP 0x%02X: failed (%s)' % (c, e))
        time.sleep(0.05)
    os.close(fd)


def cmd_vcpset(bus, code, value):
    # only allow a debug code range used by our own instrumented firmware, plus brightness for testing
    if code not in (0x10,) and not (0xE0 <= code <= 0xE2):
        sys.exit('Refusing to set VCP 0x%02X (allowed: 0x10 brightness, 0xE0-0xE2 debug).' % code)
    fd = open_bus(bus)
    vcp_set(fd, code, value)
    print('set VCP 0x%02X = %d' % (code, value))
    os.close(fd)


DBG = 0xE0   # debug VCP code added by the instrumented firmware (set = address, get = byte)


def peek(fd, address, tries=4):
    """Read one byte of the scaler's XDATA space through the debug VCP code.
    Retries a few times: the monitor's CPU sometimes answers late or garbled."""
    err = None
    for _ in range(tries):
        try:
            vcp_set(fd, DBG, address)
            time.sleep(0.03)
            res, typ, mx, cur = vcp_get(fd, DBG)
            if res != 0:
                raise ValueError('debug VCP 0xE0 not supported (is the instrumented firmware flashed?)')
            if mx != address:
                raise ValueError('reply was for address 0x%04X, not 0x%04X' % (mx, address))
            return cur & 0xFF
        except (OSError, ValueError) as e:
            err = e
            time.sleep(0.1)
    raise err


POKE_OK = (0x0040, 0x0041, 0x0042, 0x0043, 0x0044)   # frame sync delays; 0x44 bit 7 selects what 0x44/0x45 report


def cmd_poke(bus, address, value):
    if address not in POKE_OK or not 0 <= value <= 0xFF:
        sys.exit('poke is limited to scaler registers %s and values 0-255.' % ', '.join('0x%04X' % a for a in POKE_OK))
    fd = open_bus(bus)
    before = peek(fd, address)
    vcp_set(fd, DBG, address)      # remember the address (VCP E0)
    time.sleep(0.03)
    vcp_set(fd, DBG + 1, value)    # write the byte (VCP E1)
    time.sleep(0.05)
    after = peek(fd, address)
    print('0x%04X: 0x%02X -> 0x%02X (read back 0x%02X)' % (address, before, value, after))
    os.close(fd)


DISP_REGS = [  # (index, name) of the display format registers behind port 0x2A/0x2B, 16 bit pairs
    (0x00, 'DH total'), (0x02, 'DH sync end'), (0x03, 'DH bkgd start'), (0x05, 'DH active start'),
    (0x07, 'DH active end'), (0x09, 'DH bkgd end'), (0x0B, 'DV total'), (0x0D, 'DV sync end'),
    (0x0E, 'DV bkgd start'), (0x10, 'DV active start'), (0x12, 'DV active end'), (0x14, 'DV bkgd end'),
]


def cmd_disp(bus):
    fd = open_bus(bus)

    def rd(i):
        vcp_set(fd, DBG, 0x002A)
        time.sleep(0.03)
        vcp_set(fd, DBG + 1, i)        # index (VCP E1 writes it to 0x2A)
        time.sleep(0.03)
        return peek(fd, 0x002B)
    for i, name in DISP_REGS:
        if name.endswith('sync end'):
            print('%-16s 0x%02X: %d' % (name, i, rd(i)))
        else:
            hi, lo = rd(i), rd(i + 1)
            print('%-16s 0x%02X: %d' % (name, i, (hi & 0x0F) << 8 | lo))
    os.close(fd)


PORT_SETS = [  # (name, index port, data port, indices) - read only, index written before every read
    ('display', 0x2A, 0x2B, range(0x00, 0x24)),
    ('highlight window', 0x60, 0x61, range(0x00, 0x0E)),
    ('scale up', 0x33, 0x34, range(0x00, 0x12)),
]


def cmd_ports(bus):
    fd = open_bus(bus)
    for name, ip, dp, idx in PORT_SETS:
        vals = []
        for i in idx:
            try:
                vcp_set(fd, DBG, ip)
                time.sleep(0.03)
                vcp_set(fd, DBG + 1, i)
                time.sleep(0.03)
                vals.append('%02X' % peek(fd, dp))
            except (OSError, ValueError):
                vals.append('--')
        print('%s (0x%02X/0x%02X):' % (name, ip, dp))
        for k in range(0, len(vals), 16):
            print('  %02X: %s' % (idx[0] + k, ' '.join(vals[k:k + 16])))
    os.close(fd)


def cmd_dispset(bus, name, value):
    """Move the vertical active window (start and end together, same height) or the background
    window. Writes only display registers 0x0E-0x15, through the 0x2A/0x2B port."""
    base = {'vact': 0x10, 'vbg': 0x0E}.get(name)
    if base is None or not 0 <= value <= 1200:
        sys.exit("dispset: NAME is 'vact' or 'vbg', VALUE 0-1200.")
    fd = open_bus(bus)

    def wr(i, v):
        vcp_set(fd, DBG, 0x002A)
        time.sleep(0.03)
        vcp_set(fd, DBG + 1, i)
        time.sleep(0.03)
        vcp_set(fd, DBG, 0x002B)
        time.sleep(0.03)
        vcp_set(fd, DBG + 1, v)
        time.sleep(0.03)

    def rd(i):
        vcp_set(fd, DBG, 0x002A)
        time.sleep(0.03)
        vcp_set(fd, DBG + 1, i)
        time.sleep(0.03)
        return peek(fd, 0x002B)
    end_i = base + 2 if name == 'vact' else 0x14
    raw = {i: rd(i) for i in (base, base + 1, end_i, end_i + 1)}   # read once, then only write
    start = (raw[base] & 0x0F) << 8 | raw[base + 1]
    end = (raw[end_i] & 0x0F) << 8 | raw[end_i + 1]
    new_end = end + value - start
    for i, v in ((base, value), (end_i, new_end)):
        wr(i, (raw[i] & 0xF0) | (v >> 8))
        wr(i + 1, v & 0xFF)
    print('%s: start %d -> %d, end %d -> %d' % (name, start, value, end, new_end))
    os.close(fd)


def cmd_peek(bus, addrs):
    fd = open_bus(bus)
    for a in addrs:
        try:
            print('0x%04X = 0x%02X' % (a, peek(fd, a)))
        except (OSError, ValueError) as e:
            print('0x%04X : failed (%s)' % (a, e))
    os.close(fd)


def cmd_watch(bus, addrs):
    fd = open_bus(bus)
    last = None
    try:
        while True:
            vals = []
            for a in addrs:
                try:
                    vals.append('%04X=%02X' % (a, peek(fd, a)))
                except (OSError, ValueError):
                    vals.append('%04X=--' % a)
            line = ' '.join(vals)
            if line != last:            # only print changes, with a timestamp
                print(time.strftime('%H:%M:%S'), line)
                sys.stdout.flush()
                last = line
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    os.close(fd)


def cmd_dump(bus, start, end, path):
    """Read XDATA RAM start..end (inclusive) into a text file, one 'ADDR VALUE' per line.
    Meant for the firmware's RAM variables (0xFB00-0xFEFF), not for scaler registers."""
    if start < 0xF800 or end > 0xFEFF or end < start:
        sys.exit('dump is limited to RAM 0xF800-0xFEFF (scaler registers can have read side effects)')
    fd = open_bus(bus)
    n = end - start + 1
    with open(path, 'w') as f:
        for i, a in enumerate(range(start, end + 1)):
            try:
                f.write('%04X %02X\n' % (a, peek(fd, a)))
            except (OSError, ValueError):
                f.write('%04X --\n' % a)
            if i % 64 == 63:
                sys.stdout.write('\r%d / %d' % (i + 1, n))
                sys.stdout.flush()
    os.close(fd)
    print('\rwrote %s (%d bytes)' % (path, n))


def cmd_diff(path_a, path_b):
    def load(p):
        out = {}
        with open(p) as f:
            for line in f:
                a, v = line.split()
                out[int(a, 16)] = v
        return out
    a, b = load(path_a), load(path_b)
    for addr in sorted(set(a) & set(b)):
        if a[addr] != b[addr] and '--' not in (a[addr], b[addr]):
            print('%04X: %s -> %s' % (addr, a[addr], b[addr]))


def cmd_poll(bus, regs):
    fd = open_bus(bus)
    try:
        while True:
            vals = []
            for r in regs:
                try:
                    vals.append('%02X=%02X' % (r, read_reg(fd, r)))
                except OSError:
                    vals.append('%02X=--' % r)
            print(time.strftime('%H:%M:%S'), ' '.join(vals))
            sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    os.close(fd)


def main():
    a = sys.argv[1:]
    if not a or a[0] in ('-h', '--help', 'help'):
        print(__doc__)
        return
    c = a[0]
    if c == 'list':
        cmd_list()
    elif c == 'scan' and len(a) == 2:
        cmd_scan(num(a[1]))
    elif c == 'get' and len(a) >= 3:
        cmd_get(num(a[1]), [num(x) for x in a[2:]])
    elif c == 'set' and len(a) == 4:
        cmd_set(num(a[1]), num(a[2]), num(a[3]))
    elif c == 'test' and len(a) == 2:
        cmd_test(num(a[1]))
    elif c == 'vcp' and len(a) >= 3:
        cmd_vcp(num(a[1]), [num(x) for x in a[2:]])
    elif c == 'vcpset' and len(a) == 4:
        cmd_vcpset(num(a[1]), num(a[2]), num(a[3]))
    elif c == 'peek' and len(a) >= 3:
        cmd_peek(num(a[1]), [num(x) for x in a[2:]])
    elif c == 'ports' and len(a) == 2:
        cmd_ports(num(a[1]))
    elif c == 'dispset' and len(a) == 4:
        cmd_dispset(num(a[1]), a[2], num(a[3]))
    elif c == 'disp' and len(a) == 2:
        cmd_disp(num(a[1]))
    elif c == 'poke' and len(a) == 4:
        cmd_poke(num(a[1]), num(a[2]), num(a[3]))
    elif c == 'watch' and len(a) >= 3:
        cmd_watch(num(a[1]), [num(x) for x in a[2:]])
    elif c == 'dump' and len(a) == 5:
        cmd_dump(num(a[1]), num(a[2]), num(a[3]), a[4])
    elif c == 'diff' and len(a) == 3:
        cmd_diff(a[1], a[2])
    elif c == 'poll' and len(a) >= 3:
        cmd_poll(num(a[1]), [num(x) for x in a[2:]])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()
