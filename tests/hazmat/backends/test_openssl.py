# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.


import itertools
import os
import subprocess
import sys
import textwrap

import pytest

from cryptography.exceptions import InternalError, _Reasons
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.hazmat.backends.openssl.ec import _sn_to_elliptic_curve
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dh, padding
from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.ciphers.modes import CBC

from ...doubles import (
    DummyAsymmetricPadding,
    DummyBlockCipherAlgorithm,
    DummyCipherAlgorithm,
    DummyHashAlgorithm,
    DummyMode,
)
from ...utils import (
    load_nist_vectors,
    load_vectors_from_file,
    raises_unsupported_algorithm,
)
from ..primitives.fixtures_rsa import RSA_KEY_2048, RSA_KEY_512


def skip_if_libre_ssl(openssl_version):
    if "LibreSSL" in openssl_version:
        pytest.skip("LibreSSL hard-codes RAND_bytes to use arc4random.")


class TestLibreSkip:
    def test_skip_no(self):
        assert skip_if_libre_ssl("OpenSSL 1.0.2h  3 May 2016") is None

    def test_skip_yes(self):
        with pytest.raises(pytest.skip.Exception):
            skip_if_libre_ssl("LibreSSL 2.1.6")


class DummyMGF(padding.MGF):
    _salt_length = 0
    _algorithm = hashes.SHA1()


class TestOpenSSL:
    def test_backend_exists(self):
        assert backend

    def test_is_default_backend(self):
        assert backend is default_backend()

    def test_openssl_version_text(self):
        """
        This test checks the value of OPENSSL_VERSION_TEXT.

        Unfortunately, this define does not appear to have a
        formal content definition, so for now we'll test to see
        if it starts with OpenSSL or LibreSSL as that appears
        to be true for every OpenSSL-alike.
        """
        version = backend.openssl_version_text()
        assert version.startswith(("OpenSSL", "LibreSSL", "BoringSSL"))

        # Verify the correspondence between these two. And do it in a way that
        # ensures coverage.
        if version.startswith("LibreSSL"):
            assert backend._lib.CRYPTOGRAPHY_IS_LIBRESSL
        if backend._lib.CRYPTOGRAPHY_IS_LIBRESSL:
            assert version.startswith("LibreSSL")

        if version.startswith("BoringSSL"):
            assert backend._lib.CRYPTOGRAPHY_IS_BORINGSSL
        if backend._lib.CRYPTOGRAPHY_IS_BORINGSSL:
            assert version.startswith("BoringSSL")

    def test_openssl_version_number(self):
        assert backend.openssl_version_number() > 0

    def test_supports_cipher(self):
        assert (
            backend.cipher_supported(DummyCipherAlgorithm(), DummyMode())
            is False
        )

    def test_register_duplicate_cipher_adapter(self):
        with pytest.raises(ValueError):
            backend.register_cipher_adapter(AES, CBC, None)

    @pytest.mark.parametrize("mode", [DummyMode(), None])
    def test_nonexistent_cipher(self, mode, backend, monkeypatch):
        # We can't use register_cipher_adapter because backend is a
        # global singleton and we want to revert the change after the test
        monkeypatch.setitem(
            backend._cipher_registry,
            (DummyCipherAlgorithm, type(mode)),
            lambda backend, cipher, mode: backend._ffi.NULL,
        )
        cipher = Cipher(
            DummyCipherAlgorithm(),
            mode,
        )
        with raises_unsupported_algorithm(_Reasons.UNSUPPORTED_CIPHER):
            cipher.encryptor()

    def test_openssl_assert(self):
        backend.openssl_assert(True)
        with pytest.raises(InternalError):
            backend.openssl_assert(False)

    def test_consume_errors(self):
        for i in range(10):
            backend._lib.ERR_put_error(
                backend._lib.ERR_LIB_EVP, 0, 0, b"test_openssl.py", -1
            )

        assert backend._lib.ERR_peek_error() != 0

        errors = backend._consume_errors()

        assert backend._lib.ERR_peek_error() == 0
        assert len(errors) == 10

    def test_ssl_ciphers_registered(self):
        meth = backend._lib.SSLv23_method()
        ctx = backend._lib.SSL_CTX_new(meth)
        assert ctx != backend._ffi.NULL
        backend._lib.SSL_CTX_free(ctx)

    def test_evp_ciphers_registered(self):
        cipher = backend._lib.EVP_get_cipherbyname(b"aes-256-cbc")
        assert cipher != backend._ffi.NULL

    def test_unknown_error_in_cipher_finalize(self):
        cipher = Cipher(AES(b"\0" * 16), CBC(b"\0" * 16), backend=backend)
        enc = cipher.encryptor()
        enc.update(b"\0")
        backend._lib.ERR_put_error(0, 0, 1, b"test_openssl.py", -1)
        with pytest.raises(InternalError):
            enc.finalize()

    def test_int_to_bn(self):
        value = (2**4242) - 4242
        bn = backend._int_to_bn(value)
        assert bn != backend._ffi.NULL
        bn = backend._ffi.gc(bn, backend._lib.BN_clear_free)

        assert bn
        assert backend._bn_to_int(bn) == value

    def test_int_to_bn_inplace(self):
        value = (2**4242) - 4242
        bn_ptr = backend._lib.BN_new()
        assert bn_ptr != backend._ffi.NULL
        bn_ptr = backend._ffi.gc(bn_ptr, backend._lib.BN_free)
        bn = backend._int_to_bn(value, bn_ptr)

        assert bn == bn_ptr
        assert backend._bn_to_int(bn_ptr) == value

    def test_bn_to_int(self):
        bn = backend._int_to_bn(0)
        assert backend._bn_to_int(bn) == 0


@pytest.mark.skipif(
    not backend._lib.CRYPTOGRAPHY_NEEDS_OSRANDOM_ENGINE,
    reason="Requires OpenSSL with ENGINE support and OpenSSL < 1.1.1d",
)
@pytest.mark.skip_fips(reason="osrandom engine disabled for FIPS")
class TestOpenSSLRandomEngine:
    def setup(self):
        # The default RAND engine is global and shared between
        # tests. We make sure that the default engine is osrandom
        # before we start each test and restore the global state to
        # that engine in teardown.
        current_default = backend._lib.ENGINE_get_default_RAND()
        name = backend._lib.ENGINE_get_name(current_default)
        assert name == backend._lib.Cryptography_osrandom_engine_name

    def teardown(self):
        # we need to reset state to being default. backend is a shared global
        # for all these tests.
        backend.activate_osrandom_engine()
        current_default = backend._lib.ENGINE_get_default_RAND()
        name = backend._lib.ENGINE_get_name(current_default)
        assert name == backend._lib.Cryptography_osrandom_engine_name

    @pytest.mark.skipif(
        sys.executable is None, reason="No Python interpreter available."
    )
    def test_osrandom_engine_is_default(self, tmpdir):
        engine_printer = textwrap.dedent(
            """
            import sys
            from cryptography.hazmat.backends.openssl.backend import backend

            e = backend._lib.ENGINE_get_default_RAND()
            name = backend._lib.ENGINE_get_name(e)
            sys.stdout.write(backend._ffi.string(name).decode('ascii'))
            res = backend._lib.ENGINE_free(e)
            assert res == 1
            """
        )
        engine_name = tmpdir.join("engine_name")

        # If we're running tests via ``python setup.py test`` in a clean
        # environment then all of our dependencies are going to be installed
        # into either the current directory or the .eggs directory. However the
        # subprocess won't know to activate these dependencies, so we'll get it
        # to do so by passing our entire sys.path into the subprocess via the
        # PYTHONPATH environment variable.
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(sys.path)

        with engine_name.open("w") as out:
            subprocess.check_call(
                [sys.executable, "-c", engine_printer],
                env=env,
                stdout=out,
                stderr=subprocess.PIPE,
            )

        osrandom_engine_name = backend._ffi.string(
            backend._lib.Cryptography_osrandom_engine_name
        )

        assert engine_name.read().encode("ascii") == osrandom_engine_name

    def test_osrandom_sanity_check(self):
        # This test serves as a check against catastrophic failure.
        buf = backend._ffi.new("unsigned char[]", 500)
        res = backend._lib.RAND_bytes(buf, 500)
        assert res == 1
        assert backend._ffi.buffer(buf)[:] != "\x00" * 500

    def test_activate_osrandom_no_default(self):
        backend.activate_builtin_random()
        e = backend._lib.ENGINE_get_default_RAND()
        assert e == backend._ffi.NULL
        backend.activate_osrandom_engine()
        e = backend._lib.ENGINE_get_default_RAND()
        name = backend._lib.ENGINE_get_name(e)
        assert name == backend._lib.Cryptography_osrandom_engine_name
        res = backend._lib.ENGINE_free(e)
        assert res == 1

    def test_activate_builtin_random(self):
        e = backend._lib.ENGINE_get_default_RAND()
        assert e != backend._ffi.NULL
        name = backend._lib.ENGINE_get_name(e)
        assert name == backend._lib.Cryptography_osrandom_engine_name
        res = backend._lib.ENGINE_free(e)
        assert res == 1
        backend.activate_builtin_random()
        e = backend._lib.ENGINE_get_default_RAND()
        assert e == backend._ffi.NULL

    def test_activate_builtin_random_already_active(self):
        backend.activate_builtin_random()
        e = backend._lib.ENGINE_get_default_RAND()
        assert e == backend._ffi.NULL
        backend.activate_builtin_random()
        e = backend._lib.ENGINE_get_default_RAND()
        assert e == backend._ffi.NULL

    def test_osrandom_engine_implementation(self):
        name = backend.osrandom_engine_implementation()
        assert name in [
            "/dev/urandom",
            "CryptGenRandom",
            "getentropy",
            "getrandom",
        ]
        if sys.platform.startswith("linux"):
            assert name in ["getrandom", "/dev/urandom"]
        if sys.platform == "darwin":
            assert name in ["getentropy"]
        if sys.platform == "win32":
            assert name == "CryptGenRandom"

    def test_activate_osrandom_already_default(self):
        e = backend._lib.ENGINE_get_default_RAND()
        name = backend._lib.ENGINE_get_name(e)
        assert name == backend._lib.Cryptography_osrandom_engine_name
        res = backend._lib.ENGINE_free(e)
        assert res == 1
        backend.activate_osrandom_engine()
        e = backend._lib.ENGINE_get_default_RAND()
        name = backend._lib.ENGINE_get_name(e)
        assert name == backend._lib.Cryptography_osrandom_engine_name
        res = backend._lib.ENGINE_free(e)
        assert res == 1


@pytest.mark.skipif(
    backend._lib.CRYPTOGRAPHY_NEEDS_OSRANDOM_ENGINE,
    reason="Requires OpenSSL without ENGINE support or OpenSSL >=1.1.1d",
)
class TestOpenSSLNoEngine:
    def test_no_engine_support(self):
        assert (
            backend._ffi.string(backend._lib.Cryptography_osrandom_engine_id)
            == b"no-engine-support"
        )
        assert (
            backend._ffi.string(backend._lib.Cryptography_osrandom_engine_name)
            == b"osrandom_engine disabled"
        )

    def test_activate_builtin_random_does_nothing(self):
        backend.activate_builtin_random()

    def test_activate_osrandom_does_nothing(self):
        backend.activate_osrandom_engine()


class TestOpenSSLRSA:
    def test_generate_rsa_parameters_supported(self):
        assert backend.generate_rsa_parameters_supported(1, 1024) is False
        assert backend.generate_rsa_parameters_supported(4, 1024) is False
        assert backend.generate_rsa_parameters_supported(3, 1024) is True
        assert backend.generate_rsa_parameters_supported(3, 511) is False

    def test_generate_bad_public_exponent(self):
        with pytest.raises(ValueError):
            backend.generate_rsa_private_key(public_exponent=1, key_size=2048)

        with pytest.raises(ValueError):
            backend.generate_rsa_private_key(public_exponent=4, key_size=2048)

    def test_cant_generate_insecure_tiny_key(self):
        with pytest.raises(ValueError):
            backend.generate_rsa_private_key(
                public_exponent=65537, key_size=511
            )

        with pytest.raises(ValueError):
            backend.generate_rsa_private_key(
                public_exponent=65537, key_size=256
            )

    def test_rsa_padding_unsupported_pss_mgf1_hash(self):
        assert (
            backend.rsa_padding_supported(
                padding.PSS(
                    mgf=padding.MGF1(DummyHashAlgorithm()), salt_length=0
                )
            )
            is False
        )

    def test_rsa_padding_unsupported(self):
        assert backend.rsa_padding_supported(DummyAsymmetricPadding()) is False

    def test_rsa_padding_supported_pkcs1v15(self):
        assert backend.rsa_padding_supported(padding.PKCS1v15()) is True

    def test_rsa_padding_supported_pss(self):
        assert (
            backend.rsa_padding_supported(
                padding.PSS(mgf=padding.MGF1(hashes.SHA1()), salt_length=0)
            )
            is True
        )

    def test_rsa_padding_supported_oaep(self):
        assert (
            backend.rsa_padding_supported(
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA1()),
                    algorithm=hashes.SHA1(),
                    label=None,
                ),
            )
            is True
        )

    def test_rsa_padding_supported_oaep_sha2_combinations(self):
        hashalgs = [
            hashes.SHA1(),
            hashes.SHA224(),
            hashes.SHA256(),
            hashes.SHA384(),
            hashes.SHA512(),
        ]
        for mgf1alg, oaepalg in itertools.product(hashalgs, hashalgs):
            assert (
                backend.rsa_padding_supported(
                    padding.OAEP(
                        mgf=padding.MGF1(algorithm=mgf1alg),
                        algorithm=oaepalg,
                        label=None,
                    ),
                )
                is True
            )

    def test_rsa_padding_unsupported_mgf(self):
        assert (
            backend.rsa_padding_supported(
                padding.OAEP(
                    mgf=DummyMGF(),
                    algorithm=hashes.SHA1(),
                    label=None,
                ),
            )
            is False
        )

        assert (
            backend.rsa_padding_supported(
                padding.PSS(mgf=DummyMGF(), salt_length=0)
            )
            is False
        )

    def test_unsupported_mgf1_hash_algorithm_md5_decrypt(self):
        private_key = RSA_KEY_512.private_key(backend)
        with raises_unsupported_algorithm(_Reasons.UNSUPPORTED_PADDING):
            private_key.decrypt(
                b"0" * 64,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.MD5()),
                    algorithm=hashes.MD5(),
                    label=None,
                ),
            )


class TestOpenSSLCMAC:
    def test_unsupported_cipher(self):
        with raises_unsupported_algorithm(_Reasons.UNSUPPORTED_CIPHER):
            backend.create_cmac_ctx(DummyBlockCipherAlgorithm(b"bad"))


class TestOpenSSLSerializationWithOpenSSL:
    def test_pem_password_cb(self):
        userdata = backend._ffi.new("CRYPTOGRAPHY_PASSWORD_DATA *")
        pw = b"abcdefg"
        password = backend._ffi.new("char []", pw)
        userdata.password = password
        userdata.length = len(pw)
        buflen = 10
        buf = backend._ffi.new("char []", buflen)
        res = backend._lib.Cryptography_pem_password_cb(
            buf, buflen, 0, userdata
        )
        assert res == len(pw)
        assert userdata.called == 1
        assert backend._ffi.buffer(buf, len(pw))[:] == pw
        assert userdata.maxsize == buflen
        assert userdata.error == 0

    def test_pem_password_cb_no_password(self):
        userdata = backend._ffi.new("CRYPTOGRAPHY_PASSWORD_DATA *")
        buflen = 10
        buf = backend._ffi.new("char []", buflen)
        res = backend._lib.Cryptography_pem_password_cb(
            buf, buflen, 0, userdata
        )
        assert res == 0
        assert userdata.error == -1

    def test_unsupported_evp_pkey_type(self):
        key = backend._create_evp_pkey_gc()
        with raises_unsupported_algorithm(None):
            backend._evp_pkey_to_private_key(
                key, unsafe_skip_rsa_key_validation=False
            )
        with raises_unsupported_algorithm(None):
            backend._evp_pkey_to_public_key(key)

    def test_very_long_pem_serialization_password(self):
        password = b"x" * 1024

        with pytest.raises(ValueError):
            load_vectors_from_file(
                os.path.join(
                    "asymmetric",
                    "Traditional_OpenSSL_Serialization",
                    "key1.pem",
                ),
                lambda pemfile: (
                    backend.load_pem_private_key(
                        pemfile.read().encode(),
                        password,
                        unsafe_skip_rsa_key_validation=False,
                    )
                ),
            )


class TestOpenSSLEllipticCurve:
    def test_sn_to_elliptic_curve_not_supported(self):
        with raises_unsupported_algorithm(_Reasons.UNSUPPORTED_ELLIPTIC_CURVE):
            _sn_to_elliptic_curve(backend, "fake")


class TestRSAPEMSerialization:
    def test_password_length_limit(self):
        password = b"x" * 1024
        key = RSA_KEY_2048.private_key(backend)
        with pytest.raises(ValueError):
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(password),
            )


@pytest.mark.skipif(
    backend._lib.Cryptography_HAS_EVP_PKEY_DHX == 1,
    reason="Requires OpenSSL without EVP_PKEY_DHX",
)
@pytest.mark.supported(
    only_if=lambda backend: backend.dh_supported(),
    skip_message="Requires DH support",
)
class TestOpenSSLDHSerialization:
    @pytest.mark.parametrize(
        "vector",
        load_vectors_from_file(
            os.path.join("asymmetric", "DH", "RFC5114.txt"), load_nist_vectors
        ),
    )
    def test_dh_serialization_with_q_unsupported(self, backend, vector):
        parameters = dh.DHParameterNumbers(
            int(vector["p"], 16), int(vector["g"], 16), int(vector["q"], 16)
        )
        public = dh.DHPublicNumbers(int(vector["ystatcavs"], 16), parameters)
        private = dh.DHPrivateNumbers(int(vector["xstatcavs"], 16), public)
        private_key = private.private_key(backend)
        public_key = private_key.public_key()
        with raises_unsupported_algorithm(_Reasons.UNSUPPORTED_SERIALIZATION):
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        with raises_unsupported_algorithm(_Reasons.UNSUPPORTED_SERIALIZATION):
            public_key.public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        with raises_unsupported_algorithm(_Reasons.UNSUPPORTED_SERIALIZATION):
            parameters.parameters(backend).parameter_bytes(
                serialization.Encoding.PEM, serialization.ParameterFormat.PKCS3
            )

    @pytest.mark.parametrize(
        ("key_path", "loader_func"),
        [
            (
                os.path.join("asymmetric", "DH", "dhkey_rfc5114_2.pem"),
                serialization.load_pem_private_key,
            ),
            (
                os.path.join("asymmetric", "DH", "dhkey_rfc5114_2.der"),
                serialization.load_der_private_key,
            ),
        ],
    )
    def test_private_load_dhx_unsupported(
        self, key_path, loader_func, backend
    ):
        key_bytes = load_vectors_from_file(
            key_path, lambda pemfile: pemfile.read(), mode="rb"
        )
        with pytest.raises(ValueError):
            loader_func(key_bytes, None, backend)

    @pytest.mark.parametrize(
        ("key_path", "loader_func"),
        [
            (
                os.path.join("asymmetric", "DH", "dhpub_rfc5114_2.pem"),
                serialization.load_pem_public_key,
            ),
            (
                os.path.join("asymmetric", "DH", "dhpub_rfc5114_2.der"),
                serialization.load_der_public_key,
            ),
        ],
    )
    def test_public_load_dhx_unsupported(self, key_path, loader_func, backend):
        key_bytes = load_vectors_from_file(
            key_path, lambda pemfile: pemfile.read(), mode="rb"
        )
        with pytest.raises(ValueError):
            loader_func(key_bytes, backend)
