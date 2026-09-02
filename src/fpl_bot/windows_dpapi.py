"""Minimal current-user Windows DPAPI protection using the standard library."""

import ctypes
import os
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DpapiProtectionError(RuntimeError):
    """Raised without secret material when Windows DPAPI protection fails."""


class _DataBlob(ctypes.Structure):
    _fields_ = (
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    )


class WindowsDpapiProtector:
    """Encrypt bytes for decryption only by the current Windows user account."""

    def protect(self, plaintext: bytes) -> bytes:
        if os.name != "nt":
            raise DpapiProtectionError("Windows DPAPI is unavailable on this operating system")
        if not plaintext:
            raise DpapiProtectionError("Windows DPAPI cannot protect an empty payload")
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError:
            raise DpapiProtectionError("Windows DPAPI could not be loaded") from None

        crypt_protect_data = crypt32.CryptProtectData
        crypt_protect_data.argtypes = (
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        crypt_protect_data.restype = wintypes.BOOL
        local_free = kernel32.LocalFree
        local_free.argtypes = (ctypes.c_void_p,)
        local_free.restype = ctypes.c_void_p

        input_buffer = (ctypes.c_ubyte * len(plaintext)).from_buffer_copy(plaintext)
        input_blob = _DataBlob(
            cbData=len(plaintext),
            pbData=ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = _DataBlob()
        try:
            succeeded = crypt_protect_data(
                ctypes.byref(input_blob),
                "FPL Bot local OAuth token handoff",
                None,
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
            if not succeeded or not output_blob.pbData or output_blob.cbData <= 0:
                raise DpapiProtectionError("Windows DPAPI encryption failed")
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            ctypes.memset(input_buffer, 0, len(plaintext))
            if output_blob.pbData:
                local_free(output_blob.pbData)

    def unprotect(self, ciphertext: bytes) -> bytes:
        """Decrypt bytes protected for the current Windows user."""

        if os.name != "nt":
            raise DpapiProtectionError("Windows DPAPI is unavailable on this operating system")
        if not ciphertext:
            raise DpapiProtectionError("Windows DPAPI cannot decrypt an empty payload")
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError:
            raise DpapiProtectionError("Windows DPAPI could not be loaded") from None

        crypt_unprotect_data = crypt32.CryptUnprotectData
        crypt_unprotect_data.argtypes = (
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        crypt_unprotect_data.restype = wintypes.BOOL
        local_free = kernel32.LocalFree
        local_free.argtypes = (ctypes.c_void_p,)
        local_free.restype = ctypes.c_void_p

        input_buffer = (ctypes.c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
        input_blob = _DataBlob(
            cbData=len(ciphertext),
            pbData=ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = _DataBlob()
        try:
            succeeded = crypt_unprotect_data(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
            if not succeeded or not output_blob.pbData or output_blob.cbData <= 0:
                raise DpapiProtectionError("Windows DPAPI decryption failed")
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            ctypes.memset(input_buffer, 0, len(ciphertext))
            if output_blob.pbData:
                ctypes.memset(output_blob.pbData, 0, output_blob.cbData)
                local_free(output_blob.pbData)
