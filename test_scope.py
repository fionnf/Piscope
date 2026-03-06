from ctypes import byref, c_byte, c_int16, c_int32, sizeof
from time import sleep
import os
import sys
import argparse
import traceback
import logging
import matplotlib

# Choose backend based on environment or CLI flag (set later)
# We'll parse args early so we can decide backend before importing pyplot

parser = argparse.ArgumentParser(description='Simple PicoScope 2000 test/capture script')
parser.add_argument('--headless', '-n', action='store_true', help='Run without GUI and save plot to a file')
parser.add_argument('--save', '-s', default='capture.png', help='Output filename when running headless')
parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')
args, _unknown = parser.parse_known_args()

# If DISPLAY is missing or --headless requested, use non-interactive backend
if args.headless or os.environ.get('DISPLAY', '') == '':
    matplotlib.use('Agg')
else:
    # prefer TkAgg when a display is available
    matplotlib.use('TkAgg')

import matplotlib.pyplot as plt

from picosdk.ps2000 import ps2000
from picosdk.functions import assert_pico2000_ok, adc2mV
from picosdk.PicoDeviceEnums import picoEnum


SAMPLES = 2000
OVERSAMPLING = 1


def get_timebase(device, wanted_time_interval):
    current_timebase = 1

    old_time_interval = None
    time_interval = c_int32(0)
    time_units = c_int16()
    max_samples = c_int32()

    while ps2000.ps2000_get_timebase(
        device.handle,
        current_timebase,
        2000,
        byref(time_interval),
        byref(time_units),
        1,
        byref(max_samples)) == 0 \
        or time_interval.value < wanted_time_interval:

        current_timebase += 1
        old_time_interval = time_interval.value

        if current_timebase.bit_length() > sizeof(c_int16) * 8:
            raise Exception('No appropriate timebase was identifiable')

    return current_timebase - 1, old_time_interval

# Configure logging
logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

logger.info("Starting test using ps2000.open_unit()...")

try:
    with ps2000.open_unit() as device:
        logger.info('Device info: %s', device.info)

        res = ps2000.ps2000_set_channel(
            device.handle,
            picoEnum.PICO_CHANNEL['PICO_CHANNEL_A'],
            True,
            picoEnum.PICO_COUPLING['PICO_DC'],
            ps2000.PS2000_VOLTAGE_RANGE['PS2000_500MV'],
        )
        assert_pico2000_ok(res)

        res = ps2000.ps2000_set_channel(
            device.handle,
            picoEnum.PICO_CHANNEL['PICO_CHANNEL_B'],
            True,
            picoEnum.PICO_COUPLING['PICO_DC'],
            ps2000.PS2000_VOLTAGE_RANGE['PS2000_50MV'],
        )
        assert_pico2000_ok(res)

        timebase_a, interval = get_timebase(device, 4_000)

        collection_time = c_int32()

        res = ps2000.ps2000_run_block(
            device.handle,
            SAMPLES,
            timebase_a,
            OVERSAMPLING,
            byref(collection_time)
        )
        assert_pico2000_ok(res)

        while ps2000.ps2000_ready(device.handle) == 0:
            sleep(0.1)

        times = (c_int32 * SAMPLES)()

        buffer_a = (c_int16 * SAMPLES)()
        buffer_b = (c_int16 * SAMPLES)()

        overflow = c_byte(0)

        res = ps2000.ps2000_get_times_and_values(
            device.handle,
            byref(times),
            byref(buffer_a),
            byref(buffer_b),
            None,
            None,
            byref(overflow),
            2,
            SAMPLES,
        )
        assert_pico2000_ok(res)

        channel_a_overflow = (overflow.value & 0b0000_0001) != 0

        ps2000.ps2000_stop(device.handle)

        channel_a_mv = adc2mV(buffer_a, ps2000.PS2000_VOLTAGE_RANGE['PS2000_500MV'], c_int16(32767))
        channel_b_mv = adc2mV(buffer_b, ps2000.PS2000_VOLTAGE_RANGE['PS2000_50MV'], c_int16(32767))

        fig, ax = plt.subplots()
        ax.set_xlabel('time/ms')
        ax.set_ylabel('voltage/mV')
        ax.plot(list(map(lambda x: x * 1e-6, times[:])), channel_a_mv[:])
        ax.plot(list(map(lambda x: x * 1e-6, times[:])), channel_b_mv[:])

        if channel_a_overflow:
            ax.text(0.01, 0.01, 'Overflow present', color='red', transform=ax.transAxes)

        if args.headless or os.environ.get('DISPLAY', '') == '':
            out = args.save
            logger.info('Headless mode - saving figure to %s', out)
            fig.savefig(out)
            logger.info('Saved to %s', out)
        else:
            logger.info('Showing plot...')
            plt.show()
            logger.info('Done.')

except Exception as e:
    logger.error('An error occurred while talking to the PicoScope device: %s', e)
    # Print full traceback for diagnostics
    traceback.print_exc()

    # Helpful diagnostic hints for common issues on Linux/Raspberry Pi
    sys.stderr.write('\nDiagnostic hints:\n')
    sys.stderr.write('- Is the PicoScope connected and visible via `lsusb`? You should see vendor:product 0ce9:1007 for many 2000-series devices.\n')
    sys.stderr.write('- If `lsusb` shows the device but the script cannot open it, you may need a udev rule to give non-root USB access. Example rule (create /etc/udev/rules.d/99-picoscope.rules):\n')
    sys.stderr.write('  SUBSYSTEM=="usb", ATTR{idVendor}=="0ce9", ATTR{idProduct}=="1007", MODE="0666", GROUP="plugdev", SYMLINK+="picoscope%n"\n')
    sys.stderr.write('- After adding the rule, run: sudo udevadm control --reload-rules && sudo udevadm trigger && unplug/replug the device.\n')
    sys.stderr.write('- As a quick test you can try running the script with sudo to see if it is a permissions issue (not recommended as a permanent fix).\n')
    sys.stderr.write('- Ensure libusb is available and that no other process is holding the device.\n')
    sys.exit(1)
