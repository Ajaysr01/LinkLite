import pytest

from app.core.base62 import (
    decode,
    decode_id_with_offset,
    encode,
    encode_id_with_offset,
)


def test_base62_basic_encoding():
    assert encode(0) == "0"
    assert encode(1) == "1"
    assert encode(61) == "Z"
    assert encode(62) == "10"


def test_base62_roundtrip():
    test_values = [0, 1, 42, 61, 62, 12345, 916132832, 10**10, 10**14]
    for val in test_values:
        encoded = encode(val)
        decoded = decode(encoded)
        assert decoded == val, f"Failed round-trip for {val}: got {decoded} from {encoded}"


def test_offset_guarantees_minimum_length():
    # ID 0 with offset 62^5 (916,132,832) should yield exactly "100000" (6 chars)
    code_0 = encode_id_with_offset(0)
    assert len(code_0) == 6
    assert code_0 == "100000"

    # Higher IDs must also have at least 6 characters
    for i in [1, 100, 1_000_000, 50_000_000_000]:
        code = encode_id_with_offset(i)
        assert len(code) >= 6
        assert decode_id_with_offset(code) == i


def test_invalid_decode_inputs():
    with pytest.raises(ValueError, match="Cannot decode empty Base62 string"):
        decode("")

    with pytest.raises(ValueError, match="Invalid character"):
        decode("abc$12")

    with pytest.raises(ValueError, match="Invalid character"):
        decode("hello world")

    with pytest.raises(ValueError, match="Cannot encode negative integers"):
        encode(-5)


def test_decode_below_offset():
    # "0" decodes to 0, which is below 916,132,832
    with pytest.raises(ValueError, match="is below sequence offset"):
        decode_id_with_offset("0")
