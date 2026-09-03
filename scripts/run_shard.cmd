@echo off
REM Usage: run_shard.cmd <shard-name>
REM   Requires a prepared list file: lists\list_<shard-name>.txt (see prep_shards.py).
REM   Set STORAGE=supabase to upload slices to Supabase Storage instead of
REM   saving them to extracted_samples\ (the default). Supabase upload mode
REM   requires SUPABASE_URL and SUPABASE_KEY environment variables.
REM   Poppler is taken from the POPPLER_BIN env var if set, then a vendored
REM   .tools\poppler copy if present, then the system PATH.
setlocal
set ROOT=%~dp0..
if defined POPPLER_BIN (
    set PATH=%POPPLER_BIN%;%PATH%
) else if exist "%ROOT%\.tools\poppler" (
    for /d %%D in ("%ROOT%\.tools\poppler\*") do (
        if exist "%%D\Library\bin" set PATH=%%D\Library\bin;%PATH%
    )
)
if "%STORAGE%"=="" set STORAGE=local
set PYTHONIOENCODING=utf-8
cd /d "%ROOT%"
if "%STORAGE%"=="supabase" (
    python scripts\extract_questions.py --file lists\list_%~1.txt --csv-out output\shard_%~1.csv --storage supabase --manifest manifests\manifest_%~1.jsonl > logs\shard_%~1.log 2>&1
) else (
    python scripts\extract_questions.py --file lists\list_%~1.txt --csv-out output\shard_%~1.csv > logs\shard_%~1.log 2>&1
)
