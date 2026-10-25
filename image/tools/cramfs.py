# SPDX-License-Identifier: MIT
"""Read and write cramfs images (little-endian, version 2), pure stdlib.

The writer mirrors util-linux mkcramfs's layout so that an unpack/repack of
an mkcramfs-built image reproduces it byte for byte:

  superblock + root inode
  directory entries in `ls -UR` order, each directory's children sorted by name
  file data, depth first, with identical files sharing one copy
  zero padding up to a 4 KiB boundary
"""

import stat
import struct
import zlib
from dataclasses import dataclass, field

MAGIC = 0x28CD3D45
SIGNATURE = b"Compressed ROMFS"
PAGE = 4096
SUPER_FMT = "<IIII16sIIII16s"  # magic size flags future sig crc edition blocks files name
SUPER_LEN = struct.calcsize(SUPER_FMT)  # 64
INODE_LEN = 12

FLAG_FSID_VERSION_2 = 0x1
FLAG_SORTED_DIRS = 0x2


@dataclass
class Node:
    name: bytes
    mode: int
    uid: int
    gid: int
    data: bytes = b""  # file contents or symlink target
    rdev: int = 0  # device nodes, sockets, fifos: cramfs stores this in `size`
    children: list = field(default_factory=list)

    @property
    def is_dir(self):
        return stat.S_ISDIR(self.mode)

    @property
    def has_data(self):
        return stat.S_ISREG(self.mode) or stat.S_ISLNK(self.mode)


@dataclass
class Image:
    root: Node
    name: bytes
    edition: int
    flags: int


def _unpack_inode(buf, off):
    mode, uid, w1, w2 = struct.unpack_from("<HHII", buf, off)
    size, gid = w1 & 0xFFFFFF, w1 >> 24
    namelen, offset = (w2 & 0x3F) * 4, (w2 >> 6) * 4
    return mode, uid, size, gid, namelen, offset


def _pack_inode(mode, uid, size, gid, namelen, offset):
    assert namelen % 4 == 0 and offset % 4 == 0
    assert size < 1 << 24 and gid < 1 << 8 and namelen // 4 < 1 << 6
    return struct.pack("<HHII", mode, uid, size | gid << 24, namelen // 4 | (offset // 4) << 6)


def _read_data(buf, offset, size):
    nblocks = (size + PAGE - 1) // PAGE
    ptrs = struct.unpack_from(f"<{nblocks}I", buf, offset)
    start = offset + 4 * nblocks
    out = bytearray()
    for end in ptrs:
        out += zlib.decompress(buf[start:end]) if end > start else bytes(min(PAGE, size - len(out)))
        start = end
    if len(out) != size:
        raise ValueError(f"data at {offset:#x}: expected {size} bytes, got {len(out)}")
    return bytes(out)


def read(buf: bytes) -> Image:
    magic, size, flags, _, sig, crc, edition, _, _, name = struct.unpack_from(SUPER_FMT, buf)
    if magic != MAGIC or sig != SIGNATURE:
        raise ValueError("not a little-endian cramfs image")
    if flags & ~(FLAG_FSID_VERSION_2 | FLAG_SORTED_DIRS):
        raise ValueError(f"unsupported cramfs flags {flags:#x}")
    zeroed = buf[:32] + b"\0\0\0\0" + buf[36:size]
    if zlib.crc32(zeroed) != crc:
        raise ValueError("cramfs CRC mismatch")

    def node_at(off, nm):
        mode, uid, sz, gid, _, offset = _unpack_inode(buf, off)
        n = Node(nm, mode, uid, gid)
        if stat.S_ISDIR(mode):
            pos, end = offset, offset + sz
            while pos < end:
                cnamelen = _unpack_inode(buf, pos)[4]
                cname = buf[pos + INODE_LEN : pos + INODE_LEN + cnamelen].rstrip(b"\0")
                n.children.append(node_at(pos, cname))
                pos += INODE_LEN + cnamelen
        elif stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            n.data = _read_data(buf, offset, sz) if sz else b""
        else:
            n.rdev = sz
        return n

    return Image(node_at(SUPER_LEN, b""), name.rstrip(b"\0"), edition, flags)


def write(img: Image) -> bytes:
    out = bytearray(SUPER_LEN + INODE_LEN)
    patches = []  # (inode position, node) whose offset field is filled in later
    nfiles = 1

    # Directory entries in mkcramfs's `ls -UR` order: write a directory's
    # entries, push its non-empty subdirectories, then pop the first of them.
    dir_offset = {}
    entries_size = {}
    stack = []
    d = img.root
    while True:
        dir_offset[id(d)] = len(out)
        pushed = []
        for c in _kids(img, d):
            nfiles += 1
            namelen = (len(c.name) + 3) & ~3
            patches.append((len(out), c))
            out += bytes(INODE_LEN) + c.name.ljust(namelen, b"\0")
            if c.is_dir and c.children:
                pushed.append(c)
        entries_size[id(d)] = len(out) - dir_offset[id(d)]
        stack += reversed(pushed)
        if not stack:
            break
        d = stack.pop()

    # File data, depth first in directory order; identical contents share.
    data_offset = {}
    seen = {}

    def emit(d):
        for c in _kids(img, d):
            if c.has_data and c.data:
                if c.data in seen:
                    data_offset[id(c)] = seen[c.data]
                else:
                    seen[c.data] = data_offset[id(c)] = _emit_data(out, c.data)
            if c.is_dir:
                emit(c)

    emit(img.root)
    out += bytes(-len(out) % PAGE)

    def inode_for(n, namelen):
        if n.is_dir:
            size = entries_size.get(id(n), 0)
            offset = dir_offset[id(n)] if size else 0
        elif n.has_data:
            size, offset = len(n.data), data_offset.get(id(n), 0)
        else:
            size, offset = n.rdev, 0
        return _pack_inode(n.mode, n.uid, size, n.gid, namelen, offset)

    out[SUPER_LEN : SUPER_LEN + INODE_LEN] = inode_for(img.root, 0)
    for pos, n in patches:
        out[pos : pos + INODE_LEN] = inode_for(n, (len(n.name) + 3) & ~3)

    blocks = sum((len(data) + PAGE - 1) // PAGE for data in seen)
    struct.pack_into(
        SUPER_FMT, out, 0, MAGIC, len(out), img.flags, 0, SIGNATURE, 0, img.edition, blocks, nfiles, img.name.ljust(16, b"\0")
    )
    struct.pack_into("<I", out, 32, zlib.crc32(out))
    return bytes(out)


def _kids(img, d):
    return sorted(d.children, key=lambda c: c.name) if img.flags & FLAG_SORTED_DIRS else d.children


def _emit_data(out, data):
    start = len(out)
    nblocks = (len(data) + PAGE - 1) // PAGE
    out += bytes(4 * nblocks)
    for i in range(nblocks):
        out += zlib.compress(data[i * PAGE : (i + 1) * PAGE], 9)
        struct.pack_into("<I", out, start + 4 * i, len(out))
    out += bytes(-len(out) % 4)
    return start
