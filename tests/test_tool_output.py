from io import StringIO

from control_plane.tool_output import redacted_output


def test_scrubs_credentials_crossing_bounded_read_and_preserves_output():
    secret = 'synthetic-private-credential'
    value = 'a' * 16380 + secret + '\nordinary message\n' + secret + ' END'
    output = ''.join(redacted_output(StringIO(value), [secret]))
    assert output == value.replace(secret, '[redacted]')
    assert ''.join(redacted_output(StringIO('normal\n'), [])) == 'normal\n'
