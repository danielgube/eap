# Añadir una nueva herramienta a `core/tools`

EAP no versiona binarios dentro de `core`. El repositorio conserva el codigo, el
manifiesto y el bootstrap; `core/bootstrap.ps1` reconstruye los payloads que falten
al ejecutar `eap.cmd` por primera vez. Todos los payloads ejecutables administrados
se alojan bajo `core/tools/<id>`; `core/commands` queda reservado al codigo propio
de los lanzadores EAP.

## Contrato minimo

Cada herramienta se declara en `core/core_tools.json`. Use un identificador y una
carpeta cortos, estables y en minusculas:

```json
{
  "id": "mi-tool",
  "displayName": "Mi Tool",
  "directory": "tools/mi-tool",
  "executables": ["mi-tool.exe"],
  "publishToEnvironmentPath": false,
  "version": "1.2.3",
  "bootstrap": {
    "requiredFiles": ["mi-tool.exe"],
    "artifacts": [
      {
        "fileName": "mi-tool-1.2.3-win-x64.zip",
        "url": "https://proveedor.example/mi-tool-1.2.3-win-x64.zip",
        "sha256": "64_caracteres_hexadecimales_en_minusculas",
        "install": {
          "type": "zip",
          "destination": "."
        }
      }
    ],
    "validation": [
      {
        "type": "command",
        "path": "mi-tool.exe",
        "arguments": ["--version"],
        "expectContains": "1.2.3"
      }
    ]
  }
}
```

`publishToEnvironmentPath` debe ser `true` solo si los procesos de los profiles
necesitan invocar la herramienta. EAP publica la carpeta que contiene cada ruta de
`executables`, sin añadir `core` ni la raiz completa de la tool. Por ejemplo,
`"executables": ["bin\\openssl.exe"]` publica `core/tools/openssl/bin`. El
runtime de EAP y las utilidades internas deben permanecer en `false`.

## Campos del bootstrap

- `requiredFiles`: archivos relativos cuya ausencia obliga a reconstruir la tool.
- `artifacts`: una o mas descargas aplicadas en orden sobre un staging vacio.
- `fileName`, `url` y `sha256`: identidad inmutable de cada descarga. No use URLs
  que apunten a `latest`; fije siempre version y checksum.
- `install.type`: `zip` extrae ZIP y wheels; `file` copia una descarga individual;
  `sevenZip` extrae con un ejecutable instalado por un artifact anterior.
- `install.destination`: destino relativo dentro de la carpeta de la tool.
- `install.target`: nombre de destino cuando `type` es `file`.
- `install.executable`: extractor relativo a la tool cuando `type` es `sevenZip`.
- `install.source`: para un ZIP con una carpeta raiz, carpeta del payload que se
  debe publicar (por ejemplo, `terminal-1.2.3`).
- `validation`: comprobaciones antes de publicar el payload. `command` comprueba
  codigo de salida y, opcionalmente, `expectContains`; `fileVersion` compara la
  propiedad `ProductVersion` con `expect`.
- `postInstall`: hoy solo existe `pythonPath`, que reescribe un archivo `._pth`
  mediante `path` y `entries`. Se usa para el CPython embebido.

Los ZIP se extraen comprobando que ninguna entrada salga del staging. Todas las
descargas se verifican con SHA-256. La carpeta definitiva solo se sustituye despues
de validar el payload completo; si el commit falla se restaura la copia anterior.

## Procedimiento

1. Elija una distribucion oficial para Windows x64 y una version concreta.
2. Descargue el artefacto una vez y calcule su hash con:

   ```powershell
   Get-FileHash .\archivo.zip -Algorithm SHA256
   ```

3. Añada la entrada a `core/core_tools.json`. Para varios ZIP superpuestos, como
   Python y Pillow, añada varios elementos a `artifacts` en el orden necesario.
4. No añada la carpeta binaria a Git. `.gitignore` mantiene `core/tools` fuera del
   repositorio; si añade codigo fuente bajo `core`, incluyalo de forma explicita
   en la lista permitida del `.gitignore`.
5. Actualice `core/version.json` y `README.md` si la herramienta forma parte de la
   version documentada de EAP.
6. Pruebe primero la instalacion forzada y luego el arranque idempotente:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\core\bootstrap.ps1 -Force
   .\eap.cmd doctor
   .\eap.cmd --version
   ```

7. Para probar una primera ejecucion real, copie solo los archivos que Git incluiria
   a otra carpeta, ejecute alli `eap.cmd` y confirme que las tools aparecen bajo
   `core/tools` sin que sea necesario instalar ninguna tool en el sistema.

Si hace falta otro formato de instalacion, añada un nuevo `install.type` a
`Install-Artifact` en `core/bootstrap.ps1`, documentelo aqui y cubra tanto el caso
de instalacion correcta como el de checksum o payload invalido.
