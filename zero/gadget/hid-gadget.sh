#!/bin/bash
# Composite USB HID gadget (keyboard=/dev/hidg0, mouse=/dev/hidg1) via libcomposite/ConfigFS.
# Requires: dtoverlay=dwc2 in /boot/firmware/config.txt and "dwc2" in /etc/modules (see README).
set -euo pipefail
G=/sys/kernel/config/usb_gadget/kvm
modprobe libcomposite

wait_nodes() {  # /dev/hidg* appear asynchronously after bind; retry instead of racing chmod
  for _ in $(seq 1 50); do [ -c /dev/hidg0 ] && [ -c /dev/hidg1 ] && return 0; sleep 0.1; done
  echo "ERROR: /dev/hidg0 or /dev/hidg1 missing after bind" >&2; return 1
}
finish() {
  wait_nodes
  # Kept as before (services run as root). TODO: udev rule KERNEL=="hidg*" GROUP="kvm" MODE="0660".
  chmod 666 /dev/hidg0 /dev/hidg1
  echo "gadget up: $(cat "$G/UDC")"; exit 0
}
if [ -d "$G" ]; then
  # Existing gadget dir is only OK if it's actually bound to a UDC.
  if [ -n "$(cat "$G/UDC" 2>/dev/null)" ]; then finish; fi
  UDC_NAME=$(ls /sys/class/udc 2>/dev/null | head -n1 || true)
  [ -n "$UDC_NAME" ] || { echo "ERROR: gadget exists but no UDC available (dwc2 overlay?)" >&2; exit 1; }
  echo "$UDC_NAME" > "$G/UDC"; finish
fi
mkdir -p $G && cd $G
echo 0x1d6b > idVendor   # Linux Foundation (dev/test VID; TODO: fine for hobby, not for distribution)
echo 0x0104 > idProduct  # Multifunction Composite Gadget
echo 0x0100 > bcdDevice; echo 0x0200 > bcdUSB
mkdir -p strings/0x409
echo "0001" > strings/0x409/serialnumber
echo "DIY" > strings/0x409/manufacturer
echo "PiZeroW HID KVM" > strings/0x409/product
mkdir -p configs/c.1/strings/0x409
echo "HID" > configs/c.1/strings/0x409/configuration
echo 250 > configs/c.1/MaxPower

# Keyboard: standard boot keyboard, 8-byte report [mods,0,k1..k6]
mkdir -p functions/hid.usb0
echo 1 > functions/hid.usb0/protocol; echo 1 > functions/hid.usb0/subclass; echo 8 > functions/hid.usb0/report_length
echo -ne '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' > functions/hid.usb0/report_desc

# Mouse: relative, 4-byte report [buttons(3 bits), dx, dy, wheel] (signed int8)
mkdir -p functions/hid.usb1
echo 2 > functions/hid.usb1/protocol; echo 1 > functions/hid.usb1/subclass; echo 4 > functions/hid.usb1/report_length
echo -ne '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x03\x81\x06\xc0\xc0' > functions/hid.usb1/report_desc

ln -s functions/hid.usb0 configs/c.1/
ln -s functions/hid.usb1 configs/c.1/
UDC_NAME=$(ls /sys/class/udc 2>/dev/null | head -n1 || true)
[ -n "$UDC_NAME" ] || { echo "ERROR: no UDC in /sys/class/udc (is [all] dtoverlay=dwc2 set? rebooted?)" >&2; exit 1; }
echo "$UDC_NAME" > UDC   # bind to dwc2 UDC (e.g. 20980000.usb)
[ -n "$(cat UDC)" ] || { echo "ERROR: UDC bind failed" >&2; exit 1; }
finish
