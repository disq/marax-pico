import time
import os
import ssl
import wifi
import socketpool
import microcontroller
import adafruit_minimqtt.adafruit_minimqtt as MQTT
import board
import digitalio
import json
import supervisor
import traceback

# display
import busio
import terminalio
import displayio
import vectorio
from adafruit_display_text import bitmap_label as label
from adafruit_bitmap_font import bitmap_font
from adafruit_st7789 import ST7789
import pwmio

DISPLAY_SETTINGS = (320, 240, 270, 0, 0) # width, height, rotation, rowstart, colstart # Pico Display 2.0
# DISPLAY_SETTINGS = (280, 240, 90, 20, 0) # width, height, rotation, rowstart, colstart # Aliexpress 240x280 rounded corner display
DISPLAY_PINS = (board.GP17, board.GP16, board.GP19, board.GP18, board.GP20) # CS, DC, SPI_MOSI, SPI_CLK, Backlight

# switch_a = digitalio.DigitalInOut(board.GP12)
# switch_a.switch_to_input(pull=digitalio.Pull.UP)

switch_b = digitalio.DigitalInOut(board.GP13)
switch_b.switch_to_input(pull=digitalio.Pull.UP)

# switch_x = digitalio.DigitalInOut(board.GP14)
# switch_x.switch_to_input(pull=digitalio.Pull.UP)

# switch_y = digitalio.DigitalInOut(board.GP15)
# switch_y.switch_to_input(pull=digitalio.Pull.UP)

led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT
led.value = True

mag_switch = digitalio.DigitalInOut(board.GP9)
mag_switch.switch_to_input(pull=digitalio.Pull.UP)

# https://docs.circuitpython.org/en/latest/shared-bindings/busio/
marax_uart = busio.UART(board.GP0, board.GP1, baudrate=9600, timeout=1, receiver_buffer_size=64)

led_red = pwmio.PWMOut(board.GP6)
led_green = pwmio.PWMOut(board.GP7)
led_blue = pwmio.PWMOut(board.GP8)

led_brightness = 0.1 # between 0 and 1

main_font = terminalio.FONT # initial font
main_font_scale = 3 # scale for new font loaded from main_font_file
main_font_file = None
#main_font_file = "fonts/Junction-regular-24.bdf"
#main_font_scale = 1 # scale for new font loaded from main_font_file

pump_last_off_time = supervisor.ticks_ms()
uart_logging = True
pump_reed_switch_threshold = 1200

def is_pump_on():
	global pump_last_off_time, pump_reed_switch_threshold, led

	is_on = not mag_switch.value
	if is_on:
		pump_last_off_time = None
		return (True, is_on)

	ts = supervisor.ticks_ms()
	if pump_last_off_time is None:
		pump_last_off_time = ts

	if ts - pump_last_off_time < pump_reed_switch_threshold:
		return (True, is_on) # off, but we assume on until 700ms threshold has passed

	return (False, is_on)

def duty_cycle(percent):
	return 65535 - int(percent / 100.0 * 65535.0 * led_brightness)

def set_led(r = None, g = None, b = None):
	if r is not None:
		led_red.duty_cycle = duty_cycle(r)
	if g is not None:
		led_green.duty_cycle = duty_cycle(g)
	if b is not None:
		led_blue.duty_cycle = duty_cycle(b)

set_led(0, 0, 5)

last_led_update = None
last_led_blink = False

def do_led(pump_real_val, no_water = False):
	if not no_water:
		# led reflects mag_switch
		if pump_real_val:
			set_led(100, 0, 0) 
		else:
			set_led(0, 100, 0)
		return

	global last_led_update, last_led_blink

	# no water
	ts = supervisor.ticks_ms()
	if last_led_update is None or last_led_update < ts + 250:
		last_led_update = ts
		if last_led_blink:
			set_led(100, 0, 100)
		else:
			set_led(100, 100, 0)
		last_led_blink = not last_led_blink

def connect(mqtt_client, userdata, flags, rc):
    print("Connected to MQTT Broker!")
    print("Flags: {0}\n RC: {1}".format(flags, rc))
    global led
    led.value = False

def disconnect(mqtt_client, userdata, rc):
    print("Disconnected from MQTT Broker!")
    global led
    led.value = True

def publish(mqtt_client, userdata, topic, pid):
    print("Published to {0} with PID {1}".format(topic, pid))

def setup_mqtt():
	global mqtt_client

	pool = socketpool.SocketPool(wifi.radio)

	mqtt_client = MQTT.MQTT(
	    broker=os.getenv('MQTT_SERVER'),
	    port=int(os.getenv('MQTT_PORT')),
	    username=os.getenv('MQTT_USER'),
	    password=os.getenv('MQTT_PASS'),
	    socket_pool=pool,
	    client_id='marax_pico',
	    # ssl_context=ssl.create_default_context(),
	)

	mqtt_client.on_connect = connect
	mqtt_client.on_disconnect = disconnect
	mqtt_client.on_publish = publish

def setup_display():
	global display, DISPLAY_SETTINGS, DISPLAY_PINS

	# Release any resources currently in use for the displays
	displayio.release_displays()

	spi = busio.SPI(DISPLAY_PINS[3], DISPLAY_PINS[2])
	display_bus = displayio.FourWire(spi, command=DISPLAY_PINS[1], chip_select=DISPLAY_PINS[0])
	display = ST7789(display_bus, rotation=DISPLAY_SETTINGS[2], width=DISPLAY_SETTINGS[0], height=DISPLAY_SETTINGS[1], backlight_pin=DISPLAY_PINS[4], auto_refresh=True, rowstart=DISPLAY_SETTINGS[3], colstart=DISPLAY_SETTINGS[4])

# returns the group and the label
def gfx_box(x, bg_color, start_y=5, width=100, height=20, text_color=0x000000, text=None):
	g = displayio.Group(x=x, y=start_y)
	color_palette = displayio.Palette(1)
	color_palette[0] = bg_color
	g.append(vectorio.Rectangle(pixel_shader=color_palette, width=width, height=height, x=0, y=0))
	l = label.Label(terminalio.FONT, text="" if text is None else text, color=text_color, scale=1, anchor_point=(0.5, 0.4), padding_top=2, anchored_position=(width//2,height//2))
	g.append(l)
	return (g, l)

# returns the group
def draw_border(border_color=0x00FF00, border_offset=60):
	global DISPLAY_SETTINGS

	border_offset_half = border_offset // 2

	g = displayio.Group()
	color_palette = displayio.Palette(1)
	color_palette[0] = border_color
	# g.append(vectorio.Rectangle(pixel_shader=color_palette, width=DISPLAY_SETTINGS[0], height=DISPLAY_SETTINGS[1], x=0, y=0))

	# Draw border in clockwise fashion:
	#   _      _        _         _
	#     =>    |   =>  _|   =>  |_|
	g.append(vectorio.Rectangle(pixel_shader=color_palette, width=DISPLAY_SETTINGS[0], height=border_offset_half, x=0, y=0))
	g.append(vectorio.Rectangle(pixel_shader=color_palette, width=border_offset_half, height=DISPLAY_SETTINGS[1]-border_offset, x=DISPLAY_SETTINGS[0]-border_offset_half, y=border_offset_half))
	g.append(vectorio.Rectangle(pixel_shader=color_palette, width=DISPLAY_SETTINGS[0], height=border_offset_half, x=0, y=DISPLAY_SETTINGS[1]-border_offset_half))
	g.append(vectorio.Rectangle(pixel_shader=color_palette, width=border_offset_half, height=DISPLAY_SETTINGS[1]-border_offset, x=0, y=border_offset_half))
	return g

# returns only the rect
def draw_inner(bg_color=0x0000AA, border_offset=60):
	global DISPLAY_SETTINGS

	border_offset_half = border_offset // 2

	inner_palette = displayio.Palette(1)
	inner_palette[0] = bg_color
	return vectorio.Rectangle(pixel_shader=inner_palette, width=DISPLAY_SETTINGS[0]-border_offset, height=DISPLAY_SETTINGS[1]-border_offset, x=border_offset_half, y=border_offset_half)

# returns 5 indicators, each a tuple of group, label
def prepare_indicators(border_offset=60):
	border_offset_half = border_offset // 2

	# heating=false, more red
	heating_on = gfx_box(border_offset_half, 0xCC0000, width=120) 
	heating_off = gfx_box(border_offset_half, 0xFF0000, width=120)
	heating_unknown = gfx_box(border_offset_half, 0x0088FF, width=120, text_color=0xFFFFFF)
	steam = gfx_box(170, 0xBBBBBB, width=120, text_color=0x333333)
	counter = gfx_box(220, 0xDDDDDD, width=70, text_color=0x333333, start_y=215)

	return [heating_on, heating_off, heating_unknown, steam, counter]

def update_indicators(heating_on_ind, heating_off_ind, heating_unknown_ind, steam_ind, counter_ind, steam_temp=None, temp_target=None, boiler_temp=None, heating=None, counter=None):
	# deg = "°C"
	deg = " C"

	boiler_text = "MaraX not detected" if heating is None else "?" 
	if temp_target == 0:
		boiler_text = "no water"
	if boiler_temp is not None and temp_target is not None and boiler_temp < temp_target:
		boiler_text = "%d => %d%s" % (boiler_temp, temp_target, deg)
	elif boiler_temp is not None:
		boiler_text = "%d%s" % (boiler_temp, deg)
	elif temp_target is not None:
		boiler_text = "< %d%s" % (temp_target, deg)

	if heating is None:
		heating_on_ind[0].hidden = True
		heating_off_ind[0].hidden = True
		heating_unknown_ind[0].hidden = False
		heating_unknown_ind[1].text = boiler_text
	else:
		heating_unknown_ind[0].hidden = True
		heating_off_ind[0].hidden = heating
		heating_on_ind[0].hidden = not heating

		if heating:
			heating_on_ind[1].text = boiler_text
		else:
			heating_off_ind[1].text = boiler_text

	if steam_temp is None:
		steam_ind[0].hidden = True
	else:
		steam_ind[0].hidden = False
		steam_ind[1].text = "%d%s" % (steam_temp, deg)

	if counter is None:
		counter_ind[0].hidden = True
	else:
		counter_ind[0].hidden = False
		counter_ind[1].text = str(counter)

def create_screen(border_offset=60, font_scale=None):
	border_offset_half = border_offset // 2

	border_pumping = draw_border(border_color=0x880000, border_offset=border_offset)
	border_pumping.hidden = True
	border_not_pumping = draw_border(border_color=0x0088FF, border_offset=border_offset)

	g = displayio.Group()
	g.append(border_pumping)
	g.append(border_not_pumping)

	ind = prepare_indicators(border_offset=border_offset)
	for i in ind:
		g.append(i[0])

	g.append(draw_inner(bg_color=0x0000AA, border_offset=border_offset))
	g.append(version_ind()[0])

	global main_font, main_font_scale
	if font_scale is None:
		font_scale = main_font_scale

	label_last = label.Label(main_font, text="", color=0xFFFFFF, scale=font_scale, anchor_point=(0.5,0.3), anchored_position=(160, 140))
	g.append(label_last)

	label_main = label.Label(main_font, text="", color=0xFFFF00, scale=font_scale, anchor_point=(0.5,0.5), anchored_position=(160, 80))
	g.append(label_main)

	return (g, ind, border_pumping, border_not_pumping, label_main, label_last)

def pump_changed(val: boolean):
	mqtt_client.publish(os.getenv('MQTT_MARAX_PUMP_STATUS'), "on" if val else "off")

def uart_changed(data: string):
	mqtt_client.publish(os.getenv('MQTT_MARAX_UART_STATUS'), "offline" if data is None else data)

def uart_log(msg: string):
	global uart_logging
	if uart_logging:
		print(msg)

def process_uart(realtime=False, first_run=False):
	global marax_uart

#	return True, [5,10,80,True,5]

	data = [None, None, None, None, None] # steam_temp, target, boiler_temp, heating, counter value

	if first_run or marax_uart.in_waiting < 20:
		return False, data

	if not realtime and marax_uart.in_waiting > 32:
		marax_uart.reset_input_buffer()

	uart_log("uart readline...")
	line = marax_uart.readline()
	# line = "C1.23,050,140,042,1186,1"
	uart_log("uart: %s" % line)
	if line is None or len(line) == 0 or line[0] == 0:
		return False, data

	try:
		line = line.decode('ascii')
	except UnicodeError:
		print("failed to decode line:",line)
		return False, data

	uart_log("processing %s" % line)

	line_parts = line.rstrip('\r\n').rstrip('\n').split(',')
	if len(line_parts) != 6:
		print("line with unsupported number of fields: %d" % len(line_parts))
		return False, data

	valid = True

	uart_log("sw version: %s" % line_parts[0])
	try:
		data[0] = int(line_parts[1])
	except Exception as e:
		print("invalid steam_temp", line_parts[1], e)
		valid = False

	try:
		data[1] = int(line_parts[2])
	except Exception as e:
		print("invalid temp_target", line_parts[2], e)
		valid = False

	try:
		data[2] = int(line_parts[3])
	except Exception as e:
		print("invalid boiler_temp", line_parts[3], e)
		valid = False

	try:
		heating_state = int(line_parts[5])
		data[3] = True if heating_state == 1 else False if heating_state == 0 else None
		if data[3] is None:
			print("unknown heating_state", line_parts[5], heating_state)
	except Exception as e:
		print("invalid heating_state", line_parts[5], e)
		valid = False

	try:
		data[4] = int(line_parts[4])
	except Exception as e:
		print("invalid counter", line_parts[4], e)
		valid = False

	return valid, data

def version_ind():
	l = gfx_box(30, 0x0000AA, width=70, text_color=0xFFFFFF, text="MaraX-Pico", start_y=185)
	return l

def startup_screen():
	global display

	g = displayio.Group()
	border_offset = 120
	g.append(draw_border(border_color=0x0000AA, border_offset=border_offset))
	g.append(draw_inner(bg_color=0x0088FF, border_offset=border_offset))

	ind = gfx_box(60, 0x0088FF, width=200, text_color=0xFFFFFF, start_y=100)
	g.append(ind[0])
	g.append(version_ind()[0])

	display.show(g)
	return ind

def setup():
	global main_font_file, mqtt_client

	setup_display()
	ind = startup_screen()
	ind[0].hidden = False
	ind[1].text = 'Loading...'

	if main_font_file is not None:
		ind[1].label = 'Loading font...'
		main_font = bitmap_font.load_font(main_font_file)

	print("Attempting to establish WiFi connection to %s" % os.getenv('WIFI_SSID'))
	ind[1].text = 'WiFi: %s' % os.getenv('WIFI_SSID')
	wifi.radio.connect(os.getenv('WIFI_SSID'), os.getenv('WIFI_PASSWORD'), timeout=30)
	setup_mqtt()
	print("Attempting to connect to %s" % mqtt_client.broker)
	ind[1].text = 'MQTT: %s' % mqtt_client.broker
	mqtt_client.connect(keep_alive=10)

	global old_pump_val
	(old_pump_val, real_val) = is_pump_on()
	do_led(real_val)

	ind[1].text = 'OK'


def main():
	global display, old_pump_val, switch_b, mqtt_client, uart_logging

	first_run = True

	cur_time = None
	start_time = None
	last_time = None
	prev_last_time = None


	steam_temp = None
	temp_target = None
	boiler_temp = None
	heating = None
	counter = None

	label_main = None
	label_last = None

	display.auto_refresh = False

	scr = create_screen()
	display.show(scr[0])
	display.refresh()

	show_console = False

#  0   1     2             3                   4           5       
# (g, ind, border_pumping, border_not_pumping, label_main, label_last)

	last_valid_uart = supervisor.ticks_ms()
	last_mqtt_ping = last_valid_uart
	while True:
		state_changed = False

		# poll faster if pump is on
		if not old_pump_val:
			time.sleep(0.2)

			if not switch_b.value:
				show_console = not show_console
				uart_logging = not show_console
				display.show(None if show_console else scr[0])
				time.sleep(1)

		mqtt_client.loop() # maintain connection

		# Do pump
		(pump_val, pump_real_val) = is_pump_on()
		if pump_val and start_time is not None:
			cur_time = supervisor.ticks_ms() - start_time

		if pump_val is not old_pump_val:
			old_pump_val = pump_val
			pump_changed(pump_val)
			state_changed = True
			if pump_val:
				start_time = supervisor.ticks_ms()
				cur_time = 0
			else:
				last_time = cur_time - pump_reed_switch_threshold
				start_time = None
			scr[2].hidden = not pump_val
			scr[3].hidden = pump_val

		# Do UART
		uart_valid, uart_data = process_uart(pump_val, first_run)

		# Make sure we have MQTT after UART data comes back
		if supervisor.ticks_ms() - last_mqtt_ping > 5000:
			print("mqtt ping")
			try:
				mqtt_client.ping()
			except BrokenPipeError:
				mqtt_client.reconnect()
				mqtt_client.ping()
			last_mqtt_ping = supervisor.ticks_ms()

		copy_vars = uart_valid
		if uart_valid:
			last_valid_uart = supervisor.ticks_ms()
		else:
			if supervisor.ticks_ms() - last_valid_uart > 5000:
				copy_vars = True

		if copy_vars:
		# steam_temp, target, boiler_temp, heating, counter
			if steam_temp != uart_data[0]:
				steam_temp = uart_data[0]
				state_changed = True
			if temp_target != uart_data[1]:
				temp_target = uart_data[1]
				state_changed = True
			if boiler_temp != uart_data[2]:
				boiler_temp = uart_data[2]
				state_changed = True
			if heating != uart_data[3]:
				heating = uart_data[3]
				state_changed = True
			if counter != uart_data[4]:
				counter = uart_data[4]
				state_changed = True
			if state_changed:
				if steam_temp is None and temp_target is None and boiler_temp is None and heating is None and counter is None:
					uart_changed(None)
				else:
					uart_changed(json.dumps({"steam_temp":steam_temp, "temp_target":temp_target, "boiler_temp":boiler_temp, "heating":1 if heating else 0, "counter":counter}))

		do_led(pump_real_val, temp_target is not None and temp_target == 0)

		# Do screen
		if state_changed or pump_val or first_run:
			update_indicators(scr[1][0], scr[1][1], scr[1][2], scr[1][3], scr[1][4], steam_temp=steam_temp, temp_target=temp_target, boiler_temp=boiler_temp, heating=heating, counter=counter)

			if last_time is not None and prev_last_time != last_time:
				scr[5].text = "Last:%.2fs" % (last_time/1000)
				prev_last_time = last_time

			if pump_val:
				scr[4].text = "Shot: %.2fs" % (cur_time/1000)
			elif temp_target == 0 and counter == 0:
				scr[4].text = "No Water?"
			elif heating is not None:
				if boiler_temp is None or temp_target is None or temp_target < 80:
					scr[4].text = "?"
				elif boiler_temp < temp_target - 10:
					scr[4].text = "Not Ready"
				elif boiler_temp < temp_target:
					scr[4].text = "Ready%s" % (" (Heating)" if heating else "")
				else:
					scr[4].text = "READY%s" % (" (Heating)" if heating else "")
			elif heating is None:
				scr[4].text = "?"
			else:
				scr[4].text = "??" # should not happen

			first_run = False
			display.refresh()


# main
mqtt_client = None
display = None
old_pump_val = False

try:
	setup()
except Exception as e:
	display.show(None)
	print("=== Exception, will reset in 10")
	print(''.join(traceback.format_exception(None, e, e.__traceback__)))
	for i in range(10):
		if not switch_b.value:
			print("resetting now")
			break
		time.sleep(1)
	microcontroller.reset()

while True:
	try:
		main()
	except Exception as e:
		display.show(None)
		print("=== Exception, will restart in 10... B to reset")
		print(''.join(traceback.format_exception(None, e, e.__traceback__)))
		for i in range(10):
			if not switch_b.value:
				print("resetting")
				microcontroller.reset()
			time.sleep(1)
		print("restarting...")
