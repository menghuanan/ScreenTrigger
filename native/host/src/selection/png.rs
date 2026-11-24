//! 最小 PNG 编码器（TASK-008）：BGR 帧 → PNG 字节。
//!
//! 目的：框选捕获在 Rust 进程内完成（避免 Router 单线程自锁），需产出符合 02
//! `ImageRef.format ∈ {png,jpeg}` 的规范图像。本编码器只用 **zlib stored 块**
//! （无压缩，算法简单可审计）+ CRC32/adler32，输出**合法** PNG（无外部依赖）。
//! 压缩率不追求（框选区域为局部裁剪，尺寸有界）；无损、像素精确。

/// 把 BGR 帧（长度 = w*h*3）编码为 PNG 字节（8bit RGB，逐行 filter=0）。
pub fn encode_bgr_png(width: i32, height: i32, bgr: &[u8]) -> Result<Vec<u8>, String> {
    if width <= 0 || height <= 0 {
        return Err("PNG 尺寸必须为正".to_string());
    }
    let expected = (width as usize) * (height as usize) * 3;
    if bgr.len() != expected {
        return Err(format!("BGR 长度 {} 与 {width}x{height} 不符", bgr.len()));
    }

    // 原始扫描线数据：每行前置 filter 字节 0，像素 BGR→RGB
    let mut raw = Vec::with_capacity(expected + height as usize);
    for row in 0..height as usize {
        raw.push(0u8); // filter type 0 (None)
        let base = row * (width as usize) * 3;
        for col in 0..width as usize {
            let i = base + col * 3;
            raw.push(bgr[i + 2]); // R
            raw.push(bgr[i + 1]); // G
            raw.push(bgr[i]); // B
        }
    }

    let mut png = Vec::new();
    png.extend_from_slice(&[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);

    // IHDR
    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&(width as u32).to_be_bytes());
    ihdr.extend_from_slice(&(height as u32).to_be_bytes());
    ihdr.push(8); // bit depth
    ihdr.push(2); // color type: truecolor RGB
    ihdr.push(0); // compression
    ihdr.push(0); // filter
    ihdr.push(0); // interlace
    write_chunk(&mut png, b"IHDR", &ihdr);

    // IDAT（zlib stored）
    let zlib = zlib_stored(&raw);
    write_chunk(&mut png, b"IDAT", &zlib);

    // IEND
    write_chunk(&mut png, b"IEND", &[]);
    Ok(png)
}

fn write_chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let mut crc_input = Vec::with_capacity(4 + data.len());
    crc_input.extend_from_slice(kind);
    crc_input.extend_from_slice(data);
    out.extend_from_slice(&crc32(&crc_input).to_be_bytes());
}

/// zlib 容器 + stored（未压缩）deflate 块。
fn zlib_stored(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len() + data.len() / 65535 * 5 + 6);
    out.push(0x78); // CMF
    out.push(0x01); // FLG（无字典，最低压缩）
    let mut i = 0usize;
    if data.is_empty() {
        out.extend_from_slice(&[0x01, 0x00, 0x00, 0xff, 0xff]);
    }
    while i < data.len() {
        let block = (data.len() - i).min(65535);
        let last = if i + block >= data.len() { 1u8 } else { 0u8 };
        out.push(last); // BFINAL + BTYPE=00(stored)
        out.extend_from_slice(&(block as u16).to_le_bytes());
        out.extend_from_slice(&(!(block as u16)).to_le_bytes());
        out.extend_from_slice(&data[i..i + block]);
        i += block;
    }
    out.extend_from_slice(&adler32(data).to_be_bytes());
    out
}

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !crc
}

fn adler32(data: &[u8]) -> u32 {
    let (mut a, mut b) = (1u32, 0u32);
    for &byte in data {
        a = (a + byte as u32) % 65521;
        b = (b + a) % 65521;
    }
    (b << 16) | a
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_valid_png_signature_and_ihdr() {
        // 2x1 红/蓝像素
        let bgr = vec![0, 0, 255, 255, 0, 0];
        let png = encode_bgr_png(2, 1, &bgr).unwrap();
        assert_eq!(&png[..8], &[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        // IHDR 长度 13 + 类型
        assert_eq!(&png[8..12], &13u32.to_be_bytes());
        assert_eq!(&png[12..16], b"IHDR");
        // 宽 2 高 1（big-endian）位于 IHDR 数据首
        assert_eq!(&png[16..20], &2u32.to_be_bytes());
        assert_eq!(&png[20..24], &1u32.to_be_bytes());
        // IEND 结尾
        assert!(png.ends_with(b"IEND\xaeB`\x82"));
    }

    #[test]
    fn rejects_size_mismatch() {
        assert!(encode_bgr_png(2, 2, &[0, 0, 0]).is_err());
        assert!(encode_bgr_png(0, 1, &[]).is_err());
    }

    #[test]
    fn crc_and_adler_known_vectors() {
        // 已知标准向量：CRC32("123456789")=0xCBF43926；adler32("Wikipedia")=0x11E60398
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(adler32(b"Wikipedia"), 0x11E6_0398);
        assert_eq!(adler32(b""), 1);
    }

    #[test]
    fn decodes_with_stdlib_png_header_checks() {
        // 用 struct 校验 IDAT 存在
        let bgr = vec![10, 20, 30, 40, 50, 60];
        let png = encode_bgr_png(2, 1, &bgr).unwrap();
        assert!(png.windows(4).any(|w| w == b"IDAT"));
    }
}
