# Support for UC1701 (and similar) 128x64 graphics LCD displays
#
# Copyright (C) 2018-2019  Kevin O'Connor <kevin@koconnor.net>
# Copyright (C) 2018  Eric Callahan  <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from .. import bus
from . import font6x12
from .icon12x12 import ICONS

CHAR_WIDTH = 6
CHAR_HEIGHT = 12
BACKGROUND_PRIORITY_CLOCK = 0x7fffffff00000000

TextGlyphs = { 'right_arrow': b'\x1a', 'degrees': b'\xf8' }

class DisplayBase:
    def __init__(self, io, columns=128, x_offset=0):
        self.send = io.send
        # framebuffers
        self.columns = columns
        self.x_offset = x_offset
        self.vram = [bytearray(self.columns) for i in range(8)]
        self.all_framebuffers = [(self.vram[i], bytearray(b'~'*self.columns), i)
                                 for i in range(8)]
        # Cache fonts and icons in display byte order
        self.font = [self._swizzle_bits(bytearray(c))
                     for c in font6x12.VGA_FONT]
        self.icons = {}

        # Spočti vertikální odsazení (centrování 5×12 řádků na 64 px)
        display_height = len(self.vram) * 8       # =64
        rows           = display_height // CHAR_HEIGHT  # =5
        self.vertical_offset = (display_height - rows * CHAR_HEIGHT) // 2  # =2

    def flush(self):
        # Find all differences in the framebuffers and send them to the chip
        for new_data, old_data, page in self.all_framebuffers:
            if new_data == old_data:
                continue
            # Find the position of all changed bytes in this framebuffer
            diffs = [[i, 1] for i, (n, o) in enumerate(zip(new_data, old_data))
                     if n != o]
            # Batch together changes that are close to each other
            for i in range(len(diffs)-2, -1, -1):
                pos, count = diffs[i]
                nextpos, nextcount = diffs[i+1]
                if pos + 5 >= nextpos and nextcount < 16:
                    diffs[i][1] = nextcount + (nextpos - pos)
                    del diffs[i+1]
            # Transmit changes
            for col_pos, count in diffs:
                # Set Position registers
                ra = 0xb0 | (page & 0x0F)
                ca_msb = 0x10 | ((col_pos >> 4) & 0x0F)
                ca_lsb = col_pos & 0x0F
                self.send([ra, ca_msb, ca_lsb])
                # Send Data
                self.send(new_data[col_pos:col_pos+count], is_data=True)
            old_data[:] = new_data
    def _swizzle_bits(self, data):
        # Convert from "rows of pixels" format to "columns of pixels"
        top = bot = 0
        for row in range(8):
            spaced = (data[row] * 0x8040201008040201) & 0x8080808080808080
            top |= spaced >> (7 - row)
            spaced = (data[row + 8] * 0x8040201008040201) & 0x8080808080808080
            bot |= spaced >> (7 - row)
        bits_top = [(top >> s) & 0xff for s in range(0, 64, 8)]
        bits_bot = [(bot >> s) & 0xff for s in range(0, 64, 8)]
        return (bytearray(bits_top), bytearray(bits_bot))
    def set_glyphs(self, glyphs):
        for glyph_name, glyph_data in glyphs.items():
            icon = glyph_data.get('icon16x16') or glyph_data.get('icon12x12')
            if icon is not None:
                top1, bot1 = self._swizzle_bits(icon[0])
                top2, bot2 = self._swizzle_bits(icon[1])
                self.icons[glyph_name] = (top1 + top2, bot1 + bot2)
    def write_text(self, x, y, data):
        # nový pixelový režim 6×12 (21×5 znaků)
        cols, _ = self.get_dimensions()
        if x < 0:
            return
        # oříznutí textu, aby nepřetekl šířku
        data = data[: max(0, cols - x)]

        start_x = x * CHAR_WIDTH + self.x_offset
        start_y = y * CHAR_HEIGHT + self.vertical_offset

        for idx, ch in enumerate(bytearray(data)):
            # raw bitmapa znaku: 16 řádků po CHAR_WIDTH bitech
            raw = font6x12.VGA_FONT[ch]
            for row in range(CHAR_HEIGHT):
                abs_y = start_y + row
                page  = abs_y // 8
                bit   = abs_y % 8
                mask  = 1 << bit
                col_base = start_x + idx * CHAR_WIDTH
                for col in range(CHAR_WIDTH):
                    bit_val = (raw[row] >> (7 - col)) & 1
                    if bit_val:
                        self.vram[page][col_base + col] |= mask
                    else:
                        self.vram[page][col_base + col] &= ~mask

    def write_graphics(self, x, y, data):
        """XOR graphics block (16 rows) at text cell (x,y) using 6×12 grid."""
        cols, rows = self.get_dimensions()
        if x < 0 or y < 0 or x >= cols or y >= rows or len(data) != 16:
            return
        # Swizzle raw 16-row data into top/bottom column bytes
        bits_top, bits_bot = self._swizzle_bits(data)
        # Compute pixel origin
        pix_x   = x * CHAR_WIDTH + self.x_offset
        start_y = y * CHAR_HEIGHT + self.vertical_offset

        # XOR top 8px of the glyph
        page_top = start_y // 8
        for col in range(CHAR_WIDTH):
            self.vram[page_top][pix_x + col] ^= bits_top[col]

        # XOR bottom 8px of the glyph
        page_bot = (start_y + 8) // 8
        for col in range(CHAR_WIDTH):
            self.vram[page_bot][pix_x + col] ^= bits_bot[col]

    def write_glyph(self, x, y, glyph_name):
        halves = ICONS.get(glyph_name)
        if halves:
            # Každá polovina je 6×12, CHAR_WIDTH=6, CHAR_HEIGHT=12
            for half_idx, rows in enumerate(halves):
                pix_x = (x + half_idx) * CHAR_WIDTH + self.x_offset
                start_y = y * CHAR_HEIGHT + self.vertical_offset
                for row in range(CHAR_HEIGHT):
                    raw = rows[row]
                    # pokud máte raw jako bytes/bytearray, odkomentujte:
                    # if isinstance(raw, (bytes, bytearray)):
                    #     raw = raw[0]
                    for col in range(CHAR_WIDTH):
                        bit_val = (raw >> (7 - col)) & 1
                        abs_y   = start_y + row
                        page    = abs_y >> 3          # řádek // 8
                        mask    = 1 << (abs_y & 7)    # bit na stránce
                        if bit_val:
                            self.vram[page][pix_x + col] |= mask
                        else:
                            self.vram[page][pix_x + col] &= ~mask
            return 2

        char = TextGlyphs.get(glyph_name)
        if char is not None:
            # Draw character
            self.write_text(x, y, char)
            return 1
        return 0

    def clear(self):
        zeros = bytearray(self.columns)
        for page in self.vram:
            page[:] = zeros
    def get_dimensions(self):
        return (21, 5)

# IO wrapper for "4 wire" spi bus (spi bus with an extra data/control line)
class SPI4wire:
    def __init__(self, config, data_pin_name):
        self.spi = bus.MCU_SPI_from_config(config, 0, default_speed=10000000)
        dc_pin = config.get(data_pin_name)
        self.mcu_dc = bus.MCU_bus_digital_out(self.spi.get_mcu(), dc_pin,
                                              self.spi.get_command_queue())
    def send(self, cmds, is_data=False):
        self.mcu_dc.update_digital_out(is_data,
                                       reqclock=BACKGROUND_PRIORITY_CLOCK)
        self.spi.spi_send(cmds, reqclock=BACKGROUND_PRIORITY_CLOCK)

# IO wrapper for i2c bus
class I2C:
    def __init__(self, config, default_addr):
        self.i2c = bus.MCU_I2C_from_config(config, default_addr=default_addr,
                                           default_speed=400000)
    def send(self, cmds, is_data=False):
        if is_data:
            hdr = 0x40
        else:
            hdr = 0x00
        cmds = bytearray(cmds)
        cmds.insert(0, hdr)
        self.i2c.i2c_write(cmds, reqclock=BACKGROUND_PRIORITY_CLOCK)

# Helper code for toggling a reset pin on startup
class ResetHelper:
    def __init__(self, pin_desc, io_bus):
        self.mcu_reset = None
        if pin_desc is None:
            return
        self.mcu_reset = bus.MCU_bus_digital_out(io_bus.get_mcu(), pin_desc,
                                                 io_bus.get_command_queue())
    def init(self):
        if self.mcu_reset is None:
            return
        mcu = self.mcu_reset.get_mcu()
        curtime = mcu.get_printer().get_reactor().monotonic()
        print_time = mcu.estimated_print_time(curtime)
        # Toggle reset
        minclock = mcu.print_time_to_clock(print_time + .100)
        self.mcu_reset.update_digital_out(0, minclock=minclock)
        minclock = mcu.print_time_to_clock(print_time + .200)
        self.mcu_reset.update_digital_out(1, minclock=minclock)
        # Force a delay to any subsequent commands on the command queue
        minclock = mcu.print_time_to_clock(print_time + .300)
        self.mcu_reset.update_digital_out(1, minclock=minclock)

# The UC1701 is a "4-wire" SPI display device
class UC1701HD(DisplayBase):
    def __init__(self, config):
        io = SPI4wire(config, "a0_pin")
        DisplayBase.__init__(self, io)
        self.contrast = config.getint('contrast', 40, minval=0, maxval=63)
        self.reset = ResetHelper(config.get("rst_pin", None), io.spi)
    def init(self):
        self.reset.init()
        init_cmds = [0xE2, # System reset
                     0x40, # Set display to start at line 0
                     0xA0, # Set SEG direction
                     0xC8, # Set COM Direction
                     0xA2, # Set Bias = 1/9
                     0x2C, # Boost ON
                     0x2E, # Voltage regulator on
                     0x2F, # Voltage follower on
                     0xF8, # Set booster ratio
                     0x00, # Booster ratio value (4x)
                     0x23, # Set resistor ratio (3)
                     0x81, # Set Electronic Volume
                     self.contrast, # Electronic Volume value
                     0xAC, # Set static indicator off
                     0x00, # NOP
                     0xA6, # Disable Inverse
                     0xAF] # Set display enable
        self.send(init_cmds)
        self.send([0xA5]) # display all
        self.send([0xA4]) # normal display
        self.flush()

# The SSD1306 supports both i2c and "4-wire" spi
class SSD1306(DisplayBase):
    def __init__(self, config, columns=128, x_offset=0):
        cs_pin = config.get("cs_pin", None)
        if cs_pin is None:
            io = I2C(config, 60)
            io_bus = io.i2c
        else:
            io = SPI4wire(config, "dc_pin")
            io_bus = io.spi
        self.reset = ResetHelper(config.get("reset_pin", None), io_bus)
        DisplayBase.__init__(self, io, columns, x_offset)
        self.contrast = config.getint('contrast', 239, minval=0, maxval=255)
        self.vcomh = config.getint('vcomh', 0, minval=0, maxval=63)
        self.invert = config.getboolean('invert', False)
    def init(self):
        self.reset.init()
        init_cmds = [
            0xAE,       # Display off
            0xD5, 0x80, # Set oscillator frequency
            0xA8, 0x3f, # Set multiplex ratio
            0xD3, 0x00, # Set display offset
            0x40,       # Set display start line
            0x8D, 0x14, # Charge pump setting
            0x20, 0x02, # Set Memory addressing mode
            0xA1,       # Set Segment re-map
            0xC8,       # Set COM output scan direction
            0xDA, 0x12, # Set COM pins hardware configuration
            0x81, self.contrast, # Set contrast control
            0xD9, 0xA1, # Set pre-charge period
            0xDB, self.vcomh, # Set VCOMH deselect level
            0x2E,       # Deactivate scroll
            0xA4,       # Output ram to display
            0xA7 if self.invert else 0xA6, # Set normal/invert
            0xAF,       # Display on
        ]
        self.send(init_cmds)
        self.flush()

# the SH1106 is SSD1306 compatible with up to 132 columns
class SH1106(SSD1306):
    def __init__(self, config):
        x_offset = config.getint('x_offset', 0, minval=0, maxval=3)
        SSD1306.__init__(self, config, 132, x_offset=x_offset)

