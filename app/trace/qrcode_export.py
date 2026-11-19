"""二维码导出（基础设施层）。

为什么用压缩包而不是逐张接口：单批 5000 个单品码，逐张下载会产生 5000 次往返；
打成 zip 一次流式返回，页面侧只需一个下载链接（报告 5.2.1 的运行效果要求）。
"""

from __future__ import annotations

import io
import zipfile

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def _qr_png(content: str, box_size: int = 6, border: int = 2) -> bytes:
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=box_size, border=border)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_qrcode_zip(
    batch_no: str,
    codes: list[tuple[str, str]],
    base_url: str,
) -> bytes:
    """生成 zip：内含每个码的 PNG，以及一份清单 CSV（码值 → 扫码链接）。"""
    buffer = io.BytesIO()
    manifest = ["code_value,code_type,scan_url"]

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for code_value, code_type in codes:
            scan_url = f"{base_url}/trace/{code_value}"
            archive.writestr(f"{code_value}.png", _qr_png(scan_url))
            manifest.append(f"{code_value},{code_type},{scan_url}")

        archive.writestr("manifest.csv", "\n".join(manifest) + "\n")
        archive.writestr(
            "README.txt",
            f"批次 {batch_no} 溯源码二维码\n共 {len(codes)} 个\n扫码链接格式：{base_url}/trace/<码值>\n",
        )
    return buffer.getvalue()
