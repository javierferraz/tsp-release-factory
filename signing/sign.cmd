@echo off
rem Firma Authenticode con Azure Trusted Signing. Es el signCommand que la fabrica inyecta
rem en tauri build (--config), en lugar del sign.bat con rutas del portatil de cada app.
rem Rutas por variables de entorno (las pone el workflow): SIGNTOOL, TS_DLIB, TS_METADATA.
rem Credenciales: AZURE_TENANT_ID, AZURE_CLIENT_ID y AZURE_CLIENT_SECRET, que lee la DLL de
rem Azure (EnvironmentCredential). Este script no las toca ni las imprime.
setlocal
if not defined SIGNTOOL goto :faltan
if not defined TS_DLIB goto :faltan
if not defined TS_METADATA goto :faltan
"%SIGNTOOL%" sign /v /fd SHA256 /tr "http://timestamp.acs.microsoft.com" /td SHA256 /dlib "%TS_DLIB%" /dmdf "%TS_METADATA%" %1
exit /b %ERRORLEVEL%

:faltan
echo sign.cmd: faltan SIGNTOOL, TS_DLIB o TS_METADATA en el entorno
exit /b 1
