#!/usr/bin/env python3
import struct
import hashlib
import bz2
import sys
import argparse
import bsdiff4
import io
import subprocess
import os

try:
    import lzma
except ImportError:
    from backports import lzma

import update_metadata_pb2 as um

flatten = lambda l: [item for sublist in l for item in sublist]

MAGIC = b'BSDF2'

def u32(x):
    return struct.unpack('>I', x)[0]

def u64(x):
    return struct.unpack('>Q', x)[0]

def verify_contiguous(exts):
    blocks = 0

    for ext in exts:
        if ext.start_block != blocks:
            return False

        blocks += ext.num_blocks

    return True

def bsdiffpatch(tmp_buff, old_data, data):
    input_file = open('/tmp/bsdiff.input', 'wb')
    input_file.write(old_data)
    input_file.close()

    patch_file = open('/tmp/bsdiff.patch', 'wb')
    patch_file.write(data)
    patch_file.close()

    subprocess.call(["bspatcha", "/tmp/bsdiff.input", "/tmp/bsdiff.output", "/tmp/bsdiff.patch"], stdout=sys.stdout)

    output_file = open('/tmp/bsdiff.output', 'rb')
    tmp_buff.write(output_file.read())

def puffdiffpatch(tmp_buff, old_data, data):
    input_file = open('/tmp/puff.input', 'wb')
    input_file.write(old_data)
    input_file.close()

    patch_file = open('/tmp/puff.patch', 'wb')
    patch_file.write(data)
    patch_file.close()

    subprocess.call(["puffin", "--operation=puffpatch", "--src_file=/tmp/puff.input", "--dst_file=/tmp/puff.output", "--patch_file=/tmp/puff.patch"], stdout=sys.stdout)

    output_file = open('/tmp/puff.output', 'rb')
    tmp_buff.write(output_file.read())

def data_for_op(op,out_file,old_file):
    args.payloadfile.seek(data_offset + op.data_offset)
    data = args.payloadfile.read(op.data_length)

    if data[:4] == MAGIC[:4]:
        op.type = op.SOURCE_BSDIFF

    # assert hashlib.sha256(data).digest() == op.data_sha256_hash, 'operation data hash mismatch'

    if op.type == op.REPLACE_XZ:
        dec = lzma.LZMADecompressor()
        data = dec.decompress(data)
        out_file.seek(op.dst_extents[0].start_block*block_size)
        out_file.write(data)
    elif op.type == op.REPLACE_BZ:
        dec = bz2.BZ2Decompressor()
        data = dec.decompress(data)
        out_file.seek(op.dst_extents[0].start_block*block_size)
        out_file.write(data)
    elif op.type == op.REPLACE:
        out_file.seek(op.dst_extents[0].start_block*block_size)
        out_file.write(data)
    elif op.type == op.SOURCE_COPY:
        if not args.diff:
            print ("SOURCE_COPY supported only for differential OTA")
            sys.exit(-2)
        out_file.seek(op.dst_extents[0].start_block*block_size)
        for ext in op.src_extents:
            old_file.seek(ext.start_block*block_size)
            data = old_file.read(ext.num_blocks*block_size)
            out_file.write(data)
    elif op.type == op.SOURCE_BSDIFF:
        if not args.diff:
            print ("SOURCE_BSDIFF supported only for differential OTA")
            sys.exit(-3)
        out_file.seek(op.dst_extents[0].start_block*block_size)
        tmp_buff = io.BytesIO()
        for ext in op.src_extents:
            old_file.seek(ext.start_block*block_size)
            old_data = old_file.read(ext.num_blocks*block_size)
            tmp_buff.write(old_data)
        tmp_buff.seek(0)
        old_data = tmp_buff.read()
        tmp_buff.seek(0)

        bsdiffpatch(tmp_buff, old_data, data)

        n = 0;
        tmp_buff.seek(0)
        for ext in op.dst_extents:
            tmp_buff.seek(n*block_size)
            n += ext.num_blocks
            data = tmp_buff.read(ext.num_blocks*block_size)
            out_file.seek(ext.start_block*block_size)
            out_file.write(data)
    elif op.type == op.ZERO:
        for ext in op.dst_extents:
            out_file.seek(ext.start_block*block_size)
            out_file.write(b'\0' * ext.num_blocks*block_size)
    elif op.type == op.PUFFDIFF:
        if not args.diff:
            print ("PUFFDIFF supported only for differential OTA")
            sys.exit(-3)
        out_file.seek(op.dst_extents[0].start_block*block_size)
        tmp_buff = io.BytesIO()
        for ext in op.src_extents:
            old_file.seek(ext.start_block*block_size)
            old_data = old_file.read(ext.num_blocks*block_size)
            tmp_buff.write(old_data)
        tmp_buff.seek(0)
        old_data = tmp_buff.read()
        tmp_buff.seek(0)

        puffdiffpatch(tmp_buff, old_data, data)

        n = 0;
        tmp_buff.seek(0)
        for ext in op.dst_extents:
            tmp_buff.seek(n*block_size)
            n += ext.num_blocks
            data = tmp_buff.read(ext.num_blocks*block_size)
            out_file.seek(ext.start_block*block_size)
            out_file.write(data)
    else:
        print ("Unsupported type = %d" % op.type)
        sys.exit(-1)

    return data

def dump_part(part):
    sys.stdout.write("Processing %s partition" % part.partition_name)
    sys.stdout.flush()

    os.makedirs(args.out, exist_ok=True)

    out_file = open('%s/%s.img' % (args.out, part.partition_name), 'wb')
    h = hashlib.sha256()

    if args.diff:
        old_file = open('%s/%s.img' % (args.old, part.partition_name), 'rb')
    else:
        old_file = None

    for op in part.operations:
        data = data_for_op(op,out_file,old_file)
        sys.stdout.write(".")
        sys.stdout.flush()

    print("Done")


parser = argparse.ArgumentParser(description='OTA payload dumper')
parser.add_argument('payloadfile', type=argparse.FileType('rb'), 
                    help='payload file name')
parser.add_argument('--out', default='output',
                    help='output directory (defaul: output)')
parser.add_argument('--diff',action='store_true',
                    help='extract differential OTA, you need put original images to old dir')
parser.add_argument('--old', default='old',
                    help='directory with original images for differential OTA (defaul: old)')
parser.add_argument('--list', help='list payload contents',
                    action="store_true")
args = parser.parse_args()

magic = args.payloadfile.read(4)
assert magic == b'CrAU'

file_format_version = u64(args.payloadfile.read(8))
assert file_format_version == 2

manifest_size = u64(args.payloadfile.read(8))

metadata_signature_size = 0

if file_format_version > 1:
    metadata_signature_size = u32(args.payloadfile.read(4))

manifest = args.payloadfile.read(manifest_size)
metadata_signature = args.payloadfile.read(metadata_signature_size)

data_offset = args.payloadfile.tell()

dam = um.DeltaArchiveManifest()
dam.ParseFromString(manifest)
block_size = dam.block_size

for part in dam.partitions:
    # for op in part.operations:
    #     assert op.type in (op.REPLACE, op.REPLACE_BZ, op.REPLACE_XZ), \
    #             'unsupported op'

    # extents = flatten([op.dst_extents for op in part.operations])
    # assert verify_contiguous(extents), 'operations do not span full image'

    if args.list:
        print(part.partition_name)
    else:
        dump_part(part)