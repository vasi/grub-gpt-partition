#!/usr/bin/env python
import array
import os.path
import re
import struct
from subprocess import *
import sys
import tempfile

BLOCKSIZE = 512
GRUB_KERNEL_OFFSET = 0x5c

# Quote a path for debugfs
def debugfs_quote(path):
	path = os.path.realpath(path)
	return '"%s"' % path.replace('"', '""')

# Physical fs-block corresponding to start of a file
def extfs_block(device, path):
	cmd = 'bmap %s 0' % debugfs_quote(path)
	popen = Popen(['debugfs', device, '-R', cmd], stdout=PIPE, stderr=PIPE)
	block = popen.communicate()[0]
	if not block:
		return None
	return int(block)

# Field from filesystem superblock, as string
def dumpe2fs_field(device, field):
	popen = Popen(['dumpe2fs', '-h', device], stdout=PIPE, stderr=PIPE)
	output = popen.communicate()[0]
	for (f,v) in re.findall(r'^([^:]+):\s*(.*)', output, flags=re.MULTILINE):
		if f == field:
			return v

# Size of a filesystem block in bytes
def extfs_block_size(device):
	s = dumpe2fs_field(device, 'Block size')
	if not s:
		return None
	return int(s)

# Find sysfs block path for a device
def sysfs(blockdev):
	blockdev = os.path.basename(blockdev)
	return os.path.realpath(os.path.join('/sys/class/block', blockdev))

# Offset of partition in device, in 512-byte blocks
def part_offset(device):
	start = os.path.join(sysfs(device), 'start')
	with open(start) as f:
		return int(f.read())

# Flush fs buffers
def sync():
	check_call('sync')

# Get device whose filesystem contains a given path
def path_device(path):
	st = os.lstat(path)
	dev = st.st_dev
	major, minor = os.major(dev), os.minor(dev)
	syspath = os.path.realpath('/sys/dev/block/%d:%d' % (major, minor))
	return os.path.join('/dev', os.path.basename(syspath))

# Offset on disk of a file, in 512-byte blocks
def disk_offset(path):
	dev = path_device(path)
	part = extfs_block(dev, path) * (extfs_block_size(dev) / BLOCKSIZE)
	return part_offset(dev) + part

# Find the BIOS boot partition somewhere in the given devices
def bios_boot_partition(devnames):
	BIOS_BOOT_TYPE='21686148-6449-6E6F-744E-656564454649'
	for devname in devnames:
		for root, dirs, files in os.walk(sysfs(devname)):
			if 'dev' not in files:
				continue
			dev = os.path.basename(root)
			popen = Popen(['udevadm', 'info', '--query', 'all', '--name', dev],
				stdout=PIPE)
			props = popen.communicate()[0]
			if re.search(r"type.*=%s" % BIOS_BOOT_TYPE, props,
					flags = re.IGNORECASE):
				return '/dev/%s' % dev

# Device name containing a partition
def part_disk(device):
	d = sysfs(device)
	while True:
		p = os.path.dirname(d)
		if not os.path.exists(os.path.join(p, 'dev')):
			return '/dev/%s' % os.path.basename(d)
		d = p

# Modify some bootcode to point at a BIOS boot partition
def grub_fixup_bootcode(sector, core_offset, bbp_offset):
	off = struct.unpack_from('<Q', sector, offset=GRUB_KERNEL_OFFSET)[0]
	if off == core_offset: # Maybe it's already fixed!
		struct.pack_into('<Q', sector, GRUB_KERNEL_OFFSET, bbp_offset)
	return sector, off

# Modify and write fixed bootcode on a device
def grub_write_bootcode(boot_device):
	sync()
	boot = array.array('c')
	with open(boot_device) as f:
		boot.fromfile(f, BLOCKSIZE)
	core_offset = disk_offset('/boot/grub/core.img')
	bbp_offset = part_offset(bios_boot_partition([part_disk(boot_device)]))
	boot, orig_off = grub_fixup_bootcode(boot, core_offset, bbp_offset)
	
	if orig_off != core_offset:
		if orig_off == bbp_offset:
			print >>sys.stderr, "Boot sector is already ok!"
		else:
			print >>sys.stderr, "Boot sector isn't pointing at core.img, abort"
		sys.exit(1)
	
	with open(boot_device, 'w') as f:
		boot.tofile(f)

def fake_grub_setup():
	file = tempfile.NamedTemporaryFile(delete=False)
	file.write("""#!/bin/sh
grub-setup --skip-fs-probe "$@"
""")
	os.fchmod(file.fileno(), 320) # 0500 octal
	file.close()
	return file.name

# Install grub bootcode onto the boot device AND the BIOS boot partition
def grub_install(boot_device):
	# Save the MBR
	disk = part_disk(boot_device)
	with open(disk) as f:
		mbr = f.read(BLOCKSIZE)
	# Install grub to MBR, so it will initialize the BIOS boot part
	check_call(['grub-install', disk], stderr=PIPE, stdout=PIPE)
	# Restore the MBR
	with open(disk, 'w') as f:
		f.write(mbr)
	
	# Install boot code to partition
	setup = fake_grub_setup()
	check_call(['grub-install', '--force', '--grub-setup', setup,
		boot_device], stderr=PIPE, stdout=PIPE)
	os.unlink(setup)

# Make grub install boot code on the given partition, but use the
# BIOS Boot Partition for embedding
def grub_gpt_partition(boot_device):
	grub_install(boot_device)
	grub_write_bootcode(boot_device)

grub_gpt_partition(sys.argv[1])

