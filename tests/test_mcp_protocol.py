import pytest

from scripts.mcp_protocol import (
    JsonRpcError,
    decode_request,
    encode_message,
    error_response,
    result_response,
)


def test_decode_request_parses_valid_mcp_request() -> None:
    # Given
    raw = '{"jsonrpc":"2.0","id":"request-1","method":"ping","params":{}}'

    # When
    request = decode_request(raw)

    # Then
    assert request.method == "ping"
    assert request.request_id == "request-1"
    assert request.is_notification is False


def test_decode_request_reports_json_parse_error() -> None:
    # Given
    raw = "{"

    # When
    with pytest.raises(JsonRpcError) as raised:
        _ = decode_request(raw)

    # Then
    assert raised.value.code == -32700
    assert raised.value.message == "Parse error"


def test_decode_request_rejects_non_object_message() -> None:
    # Given
    raw = "[]"

    # When
    with pytest.raises(JsonRpcError) as raised:
        _ = decode_request(raw)

    # Then
    assert raised.value.code == -32600


def test_decode_request_rejects_duplicate_members_and_oversized_line() -> None:
    # Given
    duplicate = '{"jsonrpc":"2.0","id":1,"method":"ping","method":"ping"}'
    oversized = "{" + "x" * 1_048_576 + "}"

    # When / Then
    with pytest.raises(JsonRpcError) as duplicate_error:
        _ = decode_request(duplicate)
    with pytest.raises(JsonRpcError) as oversized_error:
        _ = decode_request(oversized)
    assert duplicate_error.value.code == -32600
    assert oversized_error.value.code == -32600


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (
            '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":'
            + "[0," * 16
            + "0"
            + "]" * 16
            + "}}",
            -32600,
        ),
        (
            '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":['
            + ",".join("0" for _ in range(129))
            + "]}}",
            -32600,
        ),
        (
            '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + "a" * 65_537 + '"}}',
            -32600,
        ),
        ('{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":Infinity}}', -32700),
    ],
    ids=["depth", "array", "string", "nonfinite"],
)
def test_decode_request_enforces_structural_limits(raw: str, code: int) -> None:
    # Given / When
    with pytest.raises(JsonRpcError) as raised:
        _ = decode_request(raw)

    # Then
    assert raised.value.code == code


def test_decode_request_rejects_oversized_object_member_name() -> None:
    # Given
    raw = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{' + '"' + "a" * 65_537 + '":1}}'

    # When / Then
    with pytest.raises(JsonRpcError) as raised:
        _ = decode_request(raw)
    assert raised.value.code == -32600


def test_decode_request_converts_python_digit_limit_to_parse_error() -> None:
    # Given
    raw = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"n":' + "9" * 5_000 + "}}"

    # When / Then
    with pytest.raises(JsonRpcError) as raised:
        _ = decode_request(raw)
    assert raised.value.code == -32700


def test_decode_request_preserves_id_for_invalid_params() -> None:
    # Given
    raw = '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":[]}'

    # When
    with pytest.raises(JsonRpcError) as raised:
        _ = decode_request(raw)

    # Then
    assert raised.value.code == -32600
    assert raised.value.request_id == 7


def test_json_rpc_response_helpers_emit_utf8_safe_wire_shape() -> None:
    # Given
    success = result_response("응답", {"ok": True})
    failure = error_response(JsonRpcError(-32601, "Method not found", request_id=3))

    # When
    success_wire = encode_message(success)
    failure_wire = encode_message(failure)

    # Then
    assert success_wire == '{"jsonrpc":"2.0","id":"응답","result":{"ok":true}}'
    assert failure["error"] == {"code": -32601, "message": "Method not found"}
    assert failure_wire.endswith('"message":"Method not found"}}')
