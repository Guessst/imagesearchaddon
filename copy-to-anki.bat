@echo on
set PROJECT_ROOT=C:\dev\imagesearchaddon

set ADDON_FOLDER=%USERPROFILE%\AppData\Roaming\Anki2\addons21\imagesearchaddon
if not exist %ADDON_FOLDER% mkdir %ADDON_FOLDER%

set ADDON_FILE=__init__.py
set ADDON_CONFIG_FILE=config.json
set VENDOR_FOLDER=vendor

copy "%PROJECT_ROOT%\%ADDON_FILE%" "%ADDON_FOLDER%\%ADDON_FILE%"
copy "%PROJECT_ROOT%\%ADDON_CONFIG_FILE%" "%ADDON_FOLDER%\%ADDON_CONFIG_FILE%"

robocopy "%PROJECT_ROOT%\%VENDOR_FOLDER%" "%ADDON_FOLDER%\%VENDOR_FOLDER%" /MIR /R:2 /W:5 /MT:8 /NFL /NDL /NJH /NJS

