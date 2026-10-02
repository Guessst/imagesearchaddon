if exist imagesearchaddon.zip del /f /q imagesearchaddon.zip
for /f "tokens=*" %%i in ('git describe --tags --abbrev^=0 2^>nul') do set VERSION=%%i

if not defined VERSION (
    for /f "tokens=*" %%i in ('git rev-parse --short HEAD 2^>nul') do set VERSION=%%i
)
if not defined VERSION set VERSION=latest

tar -a -cf imagesearchaddon.zip __init__.py config.json manifest.json

ren imagesearchaddon.zip imagesearchaddon-%VERSION%.ankiaddon