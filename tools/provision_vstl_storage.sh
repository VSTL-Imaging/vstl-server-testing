#!/usr/bin/env bash
set -euo pipefail

echo '[1/9] validating empty archive disk'
[[ "$(lsblk -n -o TYPE /dev/sda | tr -d ' ')" == disk ]]
[[ -z "$(lsblk -n -o NAME /dev/sda | tail -n +2)" ]]

echo '[2/9] saving current image scaffolding'
rm -rf /root/vstl-images-scaffold
mkdir -p /root/vstl-images-scaffold
cp -a /images/. /root/vstl-images-scaffold/

echo '[3/9] stopping image services'
systemctl stop nfs-server FOGMulticastManager FOGImageReplicator FOGImageSize 2>/dev/null || true

echo '[4/9] growing root to 512 GiB'
lvextend -L 512G -r /dev/ubuntu-vg/ubuntu-lv

echo '[5/9] creating dedicated FOG images LV'
lvcreate -l 100%FREE -n fog-images ubuntu-vg
mkfs.ext4 -F -m 0 -L FOG_IMAGES /dev/ubuntu-vg/fog-images

echo '[6/9] mounting FOG images'
mount /dev/ubuntu-vg/fog-images /images
cp -a /root/vstl-images-scaffold/. /images/
chown -R fogproject:fogproject /images
chmod 775 /images /images/dev /images/.mntcheck /images/postdownloadscripts
grep -q 'LABEL=FOG_IMAGES' /etc/fstab || \
  echo 'LABEL=FOG_IMAGES /images ext4 defaults,noatime 0 2' >> /etc/fstab

echo '[7/9] allocating 1.92 TB archive volume'
parted -s /dev/sda mkpart primary ext4 1MiB 100%
partprobe /dev/sda
udevadm settle
mkfs.ext4 -F -m 0 -L VSTL_ARCHIVE /dev/sda1
mkdir -p /srv/vstl-archive
mount /dev/sda1 /srv/vstl-archive
grep -q 'LABEL=VSTL_ARCHIVE' /etc/fstab || \
  echo 'LABEL=VSTL_ARCHIVE /srv/vstl-archive ext4 defaults,noatime 0 2' >> /etc/fstab
mkdir -p /srv/vstl-archive/old-server
chown -R vstl:vstl /srv/vstl-archive

echo '[8/9] reloading exports and services'
exportfs -ra
systemctl start nfs-server FOGMulticastManager FOGImageReplicator FOGImageSize 2>/dev/null || true

echo '[9/9] verification'
lvs --units t -o lv_name,vg_name,lv_size,devices
lsblk -e7 -o NAME,TYPE,SIZE,FSTYPE,LABEL,MOUNTPOINTS
df -hT / /images /srv/vstl-archive
findmnt --verify --verbose
