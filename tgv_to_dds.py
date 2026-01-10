import io
import struct
import sys
from pathlib import Path

import zstandard as zstd


def guess_fourcc(fmt: str) -> bytes:
    f = (fmt or "").upper()
    # WARNO часто має сміття в 16 байтах, тому просто шукаємо підрядки
    if "BC5" in f:
        return b"ATI2"  # BC5 (normal maps), legacy FourCC
    if "BC3" in f or "DXT5" in f:
        return b"DXT5"
    if "BC1" in f or "DXT1" in f:
        return b"DXT1"
    # fallback (краще так, ніж падати)
    return b"DXT1"


def build_dds_header(width: int, height: int, mip_count: int, top_linear_size: int, fourcc: bytes) -> bytes:
    DDS_MAGIC = b"DDS "
    DDS_HEADER_SIZE = 124
    DDS_PF_SIZE = 32

    DDSD_CAPS = 0x1
    DDSD_HEIGHT = 0x2
    DDSD_WIDTH = 0x4
    DDSD_PIXELFORMAT = 0x1000
    DDSD_MIPMAPCOUNT = 0x20000
    DDSD_LINEARSIZE = 0x80000

    DDPF_FOURCC = 0x4

    DDSCAPS_TEXTURE = 0x1000
    DDSCAPS_COMPLEX = 0x8
    DDSCAPS_MIPMAP = 0x400000

    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    caps = DDSCAPS_TEXTURE

    if mip_count > 1:
        flags |= DDSD_MIPMAPCOUNT
        caps |= DDSCAPS_COMPLEX | DDSCAPS_MIPMAP

    header = struct.pack("<I", DDS_HEADER_SIZE)
    header += struct.pack("<I", flags)
    header += struct.pack("<I", height)
    header += struct.pack("<I", width)
    header += struct.pack("<I", top_linear_size)  # dwPitchOrLinearSize (для compressed — linear size top mip)
    header += struct.pack("<I", 0)  # dwDepth
    header += struct.pack("<I", mip_count)
    header += struct.pack("<11I", *([0] * 11))  # reserved

    # DDS_PIXELFORMAT
    pf = struct.pack("<I", DDS_PF_SIZE)
    pf += struct.pack("<I", DDPF_FOURCC)
    pf += fourcc
    pf += struct.pack("<I", 0)  # dwRGBBitCount
    pf += struct.pack("<I", 0)  # dwRBitMask
    pf += struct.pack("<I", 0)  # dwGBitMask
    pf += struct.pack("<I", 0)  # dwBBitMask
    pf += struct.pack("<I", 0)  # dwABitMask
    header += pf

    # Caps
    header += struct.pack("<I", caps)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)

    return DDS_MAGIC + header


def try_table(data: bytes, table_start: int, mip_count: int):
    """Пробує прочитати offsets+sizes і рахує, скільки з них реально вказують на 'ZSTD'."""
    try:
        offsets = list(struct.unpack_from(f"<{mip_count}I", data, table_start))
        sizes = list(struct.unpack_from(f"<{mip_count}I", data, table_start + 4 * mip_count))
    except struct.error:
        return 0, None, None

    ok = 0
    for o, sz in zip(offsets, sizes):
        if 0 <= o <= len(data) - 12 and 12 <= sz <= len(data) and o + sz <= len(data) and data[o:o+4] == b"ZSTD":
            ok += 1
    return ok, offsets, sizes


def main():
    if len(sys.argv) < 2:
        print("Usage: python tgv_to_dds.py <input.tgv> [output.dds]")
        sys.exit(1)

    in_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else in_path.with_suffix(".dds")

    data = in_path.read_bytes()

    version, unk, width, height = struct.unpack_from("<4I", data, 0)
    mip_count = struct.unpack_from("<H", data, 0x18)[0]
    fmt_raw = data[0x1C:0x1C + 16].split(b"\x00")[0].decode("ascii", errors="ignore")

    fourcc = guess_fourcc(fmt_raw)

    # В WARNO зустрічаються 2 варіанти: таблиця з 0x30 або з 0x34
    best = (0, None, None, None)  # (ok, table_start, offsets, sizes)
    for ts in (0x30, 0x34, 0x38, 0x3C):
        ok, offsets, sizes = try_table(data, ts, mip_count)
        if ok > best[0]:
            best = (ok, ts, offsets, sizes)

    ok, table_start, offsets, sizes = best
    if ok == 0 or offsets is None or sizes is None:
        raise RuntimeError("Не знайшов валідну таблицю offsets/sizes (не схоже на WARNO TGV v3?)")

    print(f"File: {in_path.name}")
    print(f"Version={version} unk={unk} size={width}x{height} mipCount={mip_count} fmt={fmt_raw} -> DDS {fourcc.decode('ascii')}")
    print(f"TableStart=0x{table_start:X} (valid ZSTD entries: {ok}/{mip_count})")

    dctx = zstd.ZstdDecompressor()

    # WARNO зазвичай зберігає mips від маленького до великого.
    mips = []
    for o, sz in zip(offsets, sizes):
        if data[o:o+4] != b"ZSTD":
            continue
        raw_size = struct.unpack_from("<I", data, o + 4)[0]
        comp = data[o + 8:o + sz]

        # stream_reader не вимагає “content size” в хедері кадру — ми самі знаємо raw_size
        reader = dctx.stream_reader(io.BytesIO(comp))
        raw = reader.read(raw_size)
        reader.close()

        if len(raw) != raw_size:
            raise RuntimeError(f"Decompress size mismatch at off=0x{o:X}: got {len(raw)}, expected {raw_size}")

        mips.append((raw_size, raw))

    if not mips:
        raise RuntimeError("Не знайшов жодного ZSTD mip у файлі (дивно).")

    # Пишемо в DDS великий->малий
    mips_out = list(reversed(mips))
    header = build_dds_header(width, height, len(mips_out), mips_out[0][0], fourcc)

    with out_path.open("wb") as f:
        f.write(header)
        for _, blob in mips_out:
            f.write(blob)

    print(f"Wrote: {out_path} (mips={len(mips_out)})")


if __name__ == "__main__":
    main()
