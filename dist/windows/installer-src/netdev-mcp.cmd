@echo off
rem netdev MCP server launcher (Windows). External MCP clients point here.
setlocal
rem Force UTF-8 mode: MCP tool descriptions and payloads contain non-ASCII text.
rem The default codepage on a Chinese Windows is cp936, which makes
rem "child writes UTF-8 / parent decodes cp936" mismatches fail (JSON-RPC
rem decode errors, mojibake logs). Keep this comment ASCII-only on purpose:
rem .cmd files are read by cmd.exe using the OEM codepage, not UTF-8.
set "PYTHONUTF8=1"
set "_HERE=%~dp0"
"%_HERE%.venv\Scripts\python.exe" "%_HERE%netdev_mcp.py" %*
