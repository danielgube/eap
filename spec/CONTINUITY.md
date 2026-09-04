# Continuidad y estado verificable

## Línea base al crear este paquete

Fecha: 4 de septiembre de 2026.

### EAP

- Repositorio: `https://github.com/danielgube/eap`
- Rama: `main`
- Commit base: `b8dc0c2` (`hotfix temp cleaning with logs`)
- Rama local alineada con `origin/main` antes de añadir `spec/`.
- Versión del producto: `0.19.11`.
- Runtime: CPython Embedded 3.14.7, Pillow 12.3.0.
- Windows Terminal Portable: 1.24.11911.0.
- Suite ejecutada desde el runtime embebido: **212 pruebas correctas**.

### EAP Components

- Repositorio: `https://github.com/danielgube/eap-components`
- Rama `main` limpia y alineada con `origin/main`.
- Commit observado: `19d95eeee9528376b977b71005eeba47f136b4b0`.
- `catalogVersion`: 1.7.0.
- 18 Components: Java, Tomcat, Traefik, Maven, Git, Node.js, Python, Go, PHP,
  Bruno, DBeaver, SQL Developer, HeidiSQL, VS Code, VSCodium, Eclipse, IntelliJ
  IDEA y Kiro.
- Manifiestos actuales: `schemaVersion: 3`.

### EAP Pocketools

- Repositorio: `https://github.com/danielgube/eap-pocketools`
- Rama `main` limpia y alineada con `origin/main`.
- Commit observado: `8110c148bbfd95bbb46122af364e138ac6985bdd`.
- Pocketools: `sessionkeep` 1.0.1, `ssltruster` 1.0.0 y `zipme` 1.0.0.

Estos hashes son una referencia histórica, no ramas que deban fijarse para
siempre. Al retomar, consulte siempre `origin/main` y revise los cambios desde
esta línea base.

## Cómo retomar en otro ordenador

1. Instale Git y clone los tres repositorios, o descargue la última release de
   EAP y clone aparte los catálogos si va a desarrollarlos.
2. Abra EAP desde una ruta corta y escribible, por ejemplo `C:\eap`.
3. Ejecute `eap.cmd`; acepte el bootstrap de las Core Tools.
4. Compare `core/version.json` y los commits con esta línea base.
5. Ejecute la suite, `git diff --check` y `eap.cmd doctor`.
6. Cree `config.properties` desde la plantilla. No reutilice a ciegas un archivo
   que contenga proxies, tokens o rutas del equipo antiguo.
7. Refresque los dos repositorios públicos y pruebe listar sus contenidos.
8. Importe únicamente los paquetes de profiles que se hayan exportado de forma
   deliberada. Restaure los payloads desde sus locks o vuelva a descargarlos.
9. Reconfigure manualmente datos privados, certificados, integraciones del host
   y secretos necesarios.

Comprobaciones mínimas:

```powershell
.\core\tools\python-embed\python.exe -B -I -X utf8 -m unittest discover -s core\tests -p test_*.py
git diff --check
.\eap.cmd doctor
.\eap.cmd component repository list
.\eap.cmd pocketool repository list
```

## Qué conviene respaldar antes de formatear

Según lo que se quiera conservar:

- Código: commit y push de los tres repositorios.
- Profiles declarativos: exportar profiles desde EAP.
- Payloads para un entorno sin red: incluirlos explícitamente en la exportación.
- `custom-commands`: incluirlos explícitamente o copiarlos tras revisarlos.
- Workspace: usar su repositorio o una copia separada; EAP no lo exporta.
- Datos personales: copia separada y privada de `data/profiles`, sabiendo que
  puede contener credenciales, claves, bases de datos y cachés.
- Configuración global: revisar y copiar manualmente sólo las propiedades útiles.

No es necesario conservar `core/tools`, descargas o cachés: son reconstruibles.
No se debe subir a Git el directorio local de Codex, `auth.json`, secretos de
sandbox, bases SQLite, `config.properties` privado ni ningún `data/` real.

## Evolución resumida

- **0.15:** profiles exportables/importables, arranque degradado y restauración de
  payloads ausentes.
- **0.16:** se probó exportar configuración seleccionada de aplicaciones y se
  retiró en favor de una frontera de privacidad simple.
- **0.17–0.18:** separación de workspace y datos, vocabulario Profile,
  activar/desactivar/desinstalar Components y limpieza segura de temporales.
- **0.19.0:** bootstrap reproducible, Core Tools, integración de host, accesos
  directos, actualización y publicación de releases.
- **0.19.1–0.19.5:** Pocketools, catálogo externo de Components, nuevos IDEs y
  mejoras de bootstrap.
- **0.19.6–0.19.9:** información y rutas de Components, confianza TLS central,
  catálogo totalmente desacoplado y evolución de la interfaz.
- **0.19.10–0.19.11:** landing pública, tipos/categorías, resolvers HTML,
  extracción con 7-Zip aislada, logs y limpieza resistente a archivos Git de
  sólo lectura.

## Contexto histórico valioso ya condensado

Las tareas locales anteriores trataron, entre otros asuntos:

- profiles Java 11/21 compartiendo Maven, Git y datos;
- exportación segura y recuperación en modo degradado;
- externalización del catálogo y manuales para terceros;
- diseño y publicación directa de Pocketools desde `main`;
- proxy corporativo, confianza TLS y diagnóstico de certificados;
- integración del perfil de Firefox del host;
- accesos directos, iconos, workspaces explícitos y caché de Kiro;
- release/update transaccional y rutas largas del bootstrap;
- caída durante la extracción de SQL Developer por intervención externa;
- navegación por páginas, color y dashboard tabular;
- logs persistentes y limpieza de temporales.

Las conclusiones duraderas están en `PRODUCT.md`, `ARCHITECTURE.md` y
`DECISIONS.md`. No hace falta migrar los transcripts para conservar esas reglas.
