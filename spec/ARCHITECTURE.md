# Arquitectura de EAP

## Vista general

```text
eap.cmd
  └─ bootstrap.ps1 ──> core/tools reconstruibles
       └─ CPython embebido ──> paquete eap
            ├─ catálogos externos ──> definiciones de Components
            ├─ repos Pocketools ────> comandos globales
            ├─ profiles + locks ────> entorno efectivo
            ├─ installer/transfers ─> staging, verificación y commit
            └─ terminal/launchers ──> procesos hijos aislados
```

El core conserva las garantías. Los catálogos describen productos; no controlan
la descarga, el confinamiento, el commit ni el rollback.

## Árbol de directorios

```text
EAP_ROOT/
  eap.cmd                       entrada estable
  config.properties             configuración local, privada y no versionada
  config.properties.example     plantilla pública
  core/
    app/eap/                    código Python versionado
    bootstrap.ps1              reconstrucción del runtime interno
    core_tools.json             artefactos internos fijados y verificados
    tools/                      binarios internos reconstruibles, ignorados
    catalog/
      catalog.json              contrato base vacío de Components
      host-integrations.json    recetas permitidas de integración con el host
    tests/                      suite `unittest`
    release.json                frontera de actualización pública
    version.json                versión del producto y runtime
  components/                   payloads compartidos e inmutables
  pocketools/
    bin/                        shims globales
    packages/                   payloads versionados
  envs/<profile>/
    environment.json            selección deseada
    environment.lock.json       selección exacta y origen
    state.json                  estado operativo
    config.properties           configuración privada del profile
  data/
    eap-state.json              profile seleccionado
    profiles/<data>/            home portable y custom-commands
    component-catalogs/         snapshots externos fijados
    pocketools/                 índices, lock y estado mutable
  workspaces/<workspace>/       contenido de trabajo; no se exporta
  temp/                         descargas, staging, transacciones y locks
  logs/                         un log de consola por ejecución
  exports/                      resultados de exportación y releases
```

`components/`, `pocketools/`, `envs/`, `data/`, `workspaces/`, `temp/`, `logs/`,
`exports/` y `config.properties` son estado local y están ignorados por Git.

## Arranque

1. `eap.cmd` invoca `core/bootstrap.ps1`.
2. El bootstrap lee `core/core_tools.json`.
3. Si falta un payload interno, descarga artefactos con URL y SHA-256 fijados,
   aplica cada paso en staging, valida el resultado y lo publica.
4. El runtime CPython embebido ejecuta `eap.__main__` con modo aislado.
5. `EapApplication` asegura el layout, carga propiedades, proxy, catálogos,
   profiles, integraciones, Core Tools, Pocketools y actualización.
6. La salida estándar y de error se duplican a `logs/`, eliminando ANSI del log.

Las Core Tools vigentes son 7-Zip, mkcert, OpenSSL, CPython Embedded con Pillow,
ripgrep, los comandos EAP y Windows Terminal Portable. Sólo las herramientas
declaradas con `publishToEnvironmentPath` se exponen a los profiles.

## Configuración

`config.properties` contiene valores globales, proxies, fuentes y variables
`env.*`. El bootstrap lo crea desde la plantilla sólo si no existe y nunca lo
sobrescribe.

Cada profile tiene otro `config.properties`. Para `env.*`, la configuración del
profile gana sobre la global. EAP rechaza intentos de sobrescribir variables
reservadas o variables que pertenecen a un Component activo.

## Catálogos de Components

Las fuentes se declaran como:

```properties
components.repository.<id>=https://servidor/organizacion/repositorio
```

El repositorio oficial inicial se configura en la plantilla, no en el motor. El
gestor admite GitHub y catálogos HTTPS, fija la revisión, valida el índice y todos
los manifiestos, detecta colisiones y activa un snapshot completo sólo después de
validarlo. La caché permite seguir usando una revisión conocida sin red.

`core/catalog/catalog.json` está deliberadamente vacío. No existe ya una copia
integrada de los 18 productos oficiales. El catálogo externo vigente usa
`schemaVersion: 3`; su contrato detallado vive en `eap-components`.

Los resolvers actuales incluyen primitivas generales (`github-release-asset`,
`json-index`, `html-directory`, `html-links`, `external-executable`) y algunos
adaptadores de APIs concretas mantenidos por compatibilidad. El objetivo es que
un Component normal no requiera cambios Python.

## Instalación de Components

Flujo administrado:

1. Resolver proveedor, track y última versión compatible.
2. Validar URL, metadatos y política de verificación.
3. Reutilizar un archivo verificado o descargar a `temp/downloads`.
4. Validar ZIP antes de extraer: rutas, enlaces, nombres Windows, tamaño total y
   ratio de compresión.
5. Extraer con 7-Zip en un proceso hijo para que un fallo nativo no termine EAP.
6. Volver a comprobar que todas las entradas existen y conservan su tamaño; esto
   detecta antivirus o procesos que retiren archivos durante la extracción.
7. Validar estructura, archivos obligatorios y smoke test declarado.
8. Publicar el payload y actualizar deseado y lock de forma transaccional.

Los payloads válidos descargados pero inactivos pueden activarse sin red. Al
desinstalar, sólo se elimina un payload si ningún otro profile lo utiliza. Los
datos del usuario se conservan.

Un Component `external` no instala software: valida y fija la ruta de un
ejecutable existente en el host. Sigue usando el launcher y el entorno de EAP.

## Profiles y entorno de procesos

La definición deseada y el lock son distintos. La primera expresa intención; el
segundo contiene la versión y procedencia exactas. EAP puede arrancar en modo
degradado si el lock referencia payloads ausentes y ofrecer restaurarlos.

El entorno efectivo redefine un único home portable:

- `USERPROFILE` y `HOME`;
- `APPDATA` y `LOCALAPPDATA`;
- `TEMP`, `TMP` y `TMPDIR`;
- directorios XDG;
- `EAP_ROOT`, `EAP_PROFILE`, `EAP_DATA_PROFILE` y `EAP_WORKSPACE`.

El orden del `PATH` es:

1. rutas y comandos de Components activos;
2. `custom-commands` de los datos asociados;
3. `pocketools/bin`;
4. Core Tools publicadas;
5. `PATH` heredado del host, sin entradas EAP antiguas.

Esto permite usar una herramienta global si el usuario no activa su equivalente
EAP. Los Components ausentes en modo degradado no publican variables ni rutas.

## Pocketools

Las fuentes se declaran como:

```properties
pocketools.repository.<id>=https://github.com/organizacion/repositorio
```

Para GitHub, EAP descubre `pocketools/*/pocketool.json` en `main`, fija el commit,
inventaría blobs y descarga cada archivo desde esa revisión. No usa tags,
releases ni ZIPs. Contrasta tamaño e identificador Git y publica el paquete y sus
shims transaccionalmente.

La instalación es global. En ejecución se construye el entorno del profile
activo y se validan las capacidades requeridas. El código lee su payload mediante
`EAP_POCKETOOL_ROOT` y escribe estado únicamente en `EAP_POCKETOOL_DATA`.

## Red, proxy y confianza

EAP reconoce `http_proxy`, `https_proxy`, `all_proxy` y `no_proxy`, en mayúsculas
y minúsculas. Puede autenticar un portal mediante POST o navegador sin registrar
la contraseña. La configuración sensible no debe aparecer en locks o logs.

`trust.windows=true`, por profile, exporta las raíces de Windows a un bundle y
configura consumidores habituales manteniendo la validación activa. Node combina
la confianza del sistema y el bundle; Git, Python, pip, curl y Requests reciben
la ruta del bundle; Java usa `Windows-ROOT`. Es una política opt-in y no equivale
a aceptar certificados sin verificar.

## Integraciones con el host

Sólo se permiten recetas versionadas en `host-integrations.json`. La integración
inicial comparte el perfil roaming de Firefox mediante junction y evita enlazar
la caché local. La intención se guarda por conjunto de datos, no por profile.

Si existe un destino portable, la interfaz debe explicar la ruta y pedir permiso
antes de borrarlo. Una desactivación elimina sólo la junction, nunca el origen del
host. Las integraciones activas se muestran como `OK` o `KO`; las no activadas no
aparecen en el dashboard.

## Exportación, actualización y release

Las exportaciones usan 7-Zip y validan su contenido antes de publicar el archivo.
Los paquetes de profile no incluyen workspace ni datos. La exportación completa
de EAP puede incluir Components, pero preserva la separación entre código y
estado mutable.

`eap update` descarga el asset público, verifica SHA-256, valida la estructura y
sustituye únicamente las rutas declaradas en `core/release.json`, con rollback.
Conserva configuración, Core Tools descargadas, Components, Pocketools, profiles,
datos, workspaces y temporales.

`eap release` es administrativo y no aparece en la interfaz. Comprueba Git y
permisos, ejecuta pruebas, calcula la siguiente versión, crea commit/tag/release y
sube el ZIP y su checksum. El proceso es reanudable si una fase remota ya existe.

## Mapa de módulos

- `application.py`: fachada de casos de uso.
- `cli.py`: parser, dispatch e interfaz interactiva.
- `catalog.py`: contrato y validación de manifiestos.
- `component_repositories.py`: fuentes, snapshots y colisiones.
- `resolvers.py`: resolución de versiones y artefactos.
- `installer.py` y `zip_extraction.py`: instalación y extracción segura.
- `environments.py`: profiles, locks, datos y entorno de procesos.
- `pocketools.py`: repositorios, instalación, dependencias y ejecución.
- `transfers.py`: exportación e importación.
- `releases.py`: actualización y publicación de EAP.
- `proxy.py`: proxy y autenticación.
- `host_integrations.py`: junctions permitidas con el host.
- `terminal.py`, `shortcuts.py`, `icons.py`: terminal, launchers y accesos.
- `console_log.py`: captura persistente de consola.
- `network.py`, `locks.py`, `util.py`, `paths.py`: infraestructura común.
