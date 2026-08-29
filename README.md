# EAP

EAP crea profiles de desarrollo portables y reproducibles en Windows. El lanzador
`eap.cmd` reconstruye primero las herramientas core que falten y despues utiliza
exclusivamente su CPython embebido para administrar catalogo, descargas,
instalaciones y profiles.

Un **profile EAP** es la unidad que selecciona el usuario. Está compuesto por un
lock de componentes activos, un workspace, datos asociados y configuración
privada. El término histórico `environment` se conserva sólo en contratos
internos y como alias compatible de CLI.

## Estado actual

- motor CPython 3.14.7 embebido, aislado y sin instalación en el sistema;
- bootstrap declarativo de core con descargas verificadas y publicacion atomica;
- 7-Zip 26.02 x64 como herramienta core declarada y disponible en el `PATH`;
- Windows Terminal 1.24 portable como interfaz administrada y aislada por EAP;
- catálogo JSON general y manifiestos declarativos por componente;
- proveedores Java Temurin y Corretto;
- tracks Java 17, 21 y 25;
- Apache Maven estable con requisito de Java declarado como información;
- Git for Windows mediante su distribución oficial MinGit ZIP;
- Node.js oficial en líneas 22 LTS, 24 LTS y 26 Current;
- CPython oficial completo en líneas 3.12, 3.13 y 3.14, con pip y venv;
- DBeaver Community 26.1.x mediante el ZIP oficial para Windows x64;
- Visual Studio Code estable mediante el ZIP oficial de Microsoft;
- VSCodium estable como alternativa MIT sin telemetría de Microsoft;
- resolución de la última corrección dentro del mismo track;
- descarga HTTPS, comprobación SHA-256/SHA-512 y extracción ZIP segura;
- instalaciones inmutables y compartidas bajo components;
- lock por profile y activación de variables únicamente en procesos hijo;
- comprobación de actualizaciones sin saltos automáticos de versión mayor;
- catálogo único para instalar, actualizar, reconfigurar y lanzar componentes;
- panel responsive agrupado por tipos, con hasta dos recuadros por fila y sus datos en líneas separadas;
- color ANSI discreto para títulos, acciones y estados, desactivado automáticamente al redirigir la salida;
- título de pestaña contextual `EAP (<profile>)` para el menú y los shells;
- comando `eap` disponible en todos los procesos del profile;
- navegación inmediata con Esc para volver o cerrar el gestor sin cerrar la pestaña;
- workspace de trabajo asociado a cada profile y publicado como `EAP_WORKSPACE`;
- separación entre el workspace del proyecto y los datos mutables de aplicaciones auxiliares;
- motor de launchers declarativos con procesos GUI separados y previsualización segura;
- accesos directos `.lnk` por profile para aplicaciones arrancables;
- paquetes 7z de profile independientes de la distribución de la herramienta;
- duplicación rápida y eliminación segura de profiles sin borrar almacenamiento compartido;
- exportación e importación masiva de profiles desde opciones avanzadas;
- integraciones explícitas con el host, visibles como `OK` o `KO` en el panel principal;
- exportación 7z de EAP, con almacén de componentes opcional;
- progreso nativo de 7-Zip durante la compresión en terminales interactivas;
- configuración general y configuración privada superpuesta por profile;
- restauración de payloads ausentes usando la versión y checksum exactos del lock;
- desactivación independiente de componentes y desinstalación de payloads sin uso;
- activación local de payloads disponibles sin resolver ni descargar de nuevo;
- tamaño de temporales visible y limpieza desde la interfaz o la CLI;
- Pocketools globales desde repositorios GitHub, con versiones, ayuda, dependencias y
  comandos disponibles en todos los shells EAP;
- actualización pública y transaccional de EAP desde releases de GitHub;
- perfil de usuario portable bajo data y CLI automatizable.
- contrato `data` declarativo con directorios opcionales y archivos `if-missing`.

CPython continúa siendo un runtime privado. Los payloads ejecutables viven bajo
`core/tools`; solo las utilidades incluidas en `core/core_tools.json` que lo
indiquen expresamente pueden publicar en el `PATH` de los procesos hijos las
carpetas que contienen sus ejecutables. Así `core` no se convierte en una ruta
pública y herramientas como OpenSSL pueden exponer únicamente su subcarpeta `bin`.

## Inicio rápido

Desde CMD o PowerShell:

    C:\eap\eap.cmd

En un clon nuevo no es necesario copiar los binarios de `core`. La primera
ejecucion muestra las herramientas necesarias y solicita confirmacion antes de
descargar y verificar 7-Zip, mkcert, OpenSSL, CPython Embedded con Pillow,
ripgrep y Windows Terminal segun `core/core_tools.json`. Las siguientes
ejecuciones reutilizan las instalaciones validadas y no vuelven a solicitarla.
Para automatizar directamente el bootstrap se puede pasar `-Yes` a
`core/bootstrap.ps1`.

El procedimiento para incorporar otra utilidad esta en
[`core/add_new_core_tools.md`](core/add_new_core_tools.md).

Comandos útiles:

    eap.cmd doctor
    eap.cmd catalog
    eap.cmd profile list
    eap.cmd profile create desarrollo --workspace hbx
    eap.cmd profile create legacy --workspace legacy --data desarrollo
    eap.cmd profile duplicate java11 --profile desarrollo
    eap.cmd profile delete java11
    eap.cmd profile use desarrollo
    eap.cmd profile workspace otro-proyecto --profile desarrollo
    eap.cmd profile data desarrollo --profile legacy
    eap.cmd profile export dani --profile default
    eap.cmd profile export dani-privado --profile default --include-config
    eap.cmd profile export dani-completo --profile default --include-components
    eap.cmd profile import exports\envs\dani.7z
    eap.cmd profile export-all
    eap.cmd profile import-all
    eap.cmd profile restore --profile dani --yes
    eap.cmd tool export
    eap.cmd tool export eap-offline --include-components
    eap.cmd tool clean-temp
    eap.cmd update --check
    eap.cmd update --yes
    eap.cmd component resolve java --provider temurin --track 21
    eap.cmd component install java --provider temurin --track 21 --profile desarrollo
    eap.cmd component install maven --profile desarrollo
    eap.cmd component install git --profile desarrollo
    eap.cmd component disable git --profile desarrollo
    eap.cmd component uninstall git --profile desarrollo
    eap.cmd component install nodejs --track 24 --profile desarrollo
    eap.cmd component install python --track 3.14 --profile desarrollo
    eap.cmd component install bruno --track 4 --profile desarrollo
    eap.cmd component install dbeaver --track 26.1 --profile desarrollo
    eap.cmd component install vscode --profile desarrollo
    eap.cmd component install vscodium --profile desarrollo
    eap.cmd component install eclipse --provider java --track 2026-06 --profile desarrollo
    eap.cmd component install eclipse --provider enterprise-java --track 2026-06 --profile desarrollo
    eap.cmd component install intellij-idea --track 2026.2 --profile desarrollo
    eap.cmd component list
    eap.cmd component check-updates --profile desarrollo
    eap.cmd component update java --profile desarrollo
    eap.cmd pocketool list --available --refresh
    eap.cmd pocketool install sessionkeep
    eap.cmd pocketool help sessionkeep
    eap.cmd pocketool repository list
    eap.cmd terminal start --profile desarrollo
    eap.cmd shell --profile desarrollo --type cmd
    eap.cmd launch --profile desarrollo
    eap.cmd launch bruno --profile desarrollo
    eap.cmd launch dbeaver --profile desarrollo --dry-run
    eap.cmd launch dbeaver --profile desarrollo
    eap.cmd launch vscode --profile desarrollo
    eap.cmd launch eclipse --profile desarrollo
    eap.cmd launch intellij-idea --profile desarrollo
    eap.cmd shortcut create dbeaver --profile desarrollo
    eap.cmd shortcut create vscode --profile desarrollo

La instalación no interactiva requiere añadir --yes. Esta copia ya contiene un
profile default con Temurin 21, Maven 3.9.16, Git 2.55.0.5, Node.js 24.19.0
y Python 3.14.7, además de DBeaver Community 26.1.5 y Visual Studio Code
1.134.0, instalados como prueba integral.

## Aislamiento

core es infraestructura privada de EAP. Su ruta y la de python-embed no se añaden
al PATH en bloque ni se publican como variables de componentes. Las herramientas
core declaradas son la única excepción: se añaden de forma explícita
`core\tools\7zip`, `core\tools\mkcert`, `core\tools\openssl\bin`,
`core\tools\ripgrep` y `core\commands`. Las mismas rutas se publican mediante
`EAP_CORE_TOOLS`.

Al abrir un shell, EAP parte del entorno del proceso anfitrión, elimina cualquier
entrada que apunte a core y añade solamente las rutas declaradas por los
componentes fijados en el lock. Para Java se publica:

    JAVA_HOME=C:\eap\components\java\<proveedor>\<version>
    JAVA_TOOL_OPTIONS=-Duser.home="<datos>\home" -Djava.io.tmpdir="<datos>\home\AppData\Local\Temp"
    PATH=%JAVA_HOME%\bin;<PATH heredado sin core>

No se modifica de forma persistente el registro ni el entorno global de Windows.

## Windows Terminal

Al ejecutar `eap.cmd` sin argumentos desde una consola interactiva, EAP abre su
propia distribución portable de Windows Terminal. No reutiliza el perfil global
instalado en Windows. La primera pestaña contiene el gestor; el botón `+` abre un
CMD EAP y el desplegable ofrece perfiles EAP de CMD y PowerShell. La ventana se
abre maximizada, conservando visibles las pestañas y controles de Terminal.
En una distribución recién exportada, EAP crea primero el profile y workspace
`default` y después abre esta terminal administrada; no permanece en la consola
anfitriona usada durante el arranque.

Los ajustes se generan bajo
`data/profiles/<datos>/home/AppData/Local/Microsoft/Windows Terminal/settings.json`.
Así quedan aislados por conjunto de datos y viajan separados del core inmutable.
La instancia
recibe el profile portable completo, pero cada pestaña vuelve además a construirlo
desde el lock seleccionado. Por tanto, un cambio de profile afecta a las pestañas
nuevas sin regenerar volcados `cmd_env.bat` o `ps_env.ps1`.

El menú publica el título `EAP (<profile>)`; los shells añaden `· CMD` o
`· PowerShell`. Al pulsar Esc en la primera pestaña se cierra solamente el gestor
y queda un CMD activado del profile seleccionado. Para depuración o automatización,
`eap.cmd --inline` ejecuta el menú dentro de la terminal actual y vuelve al shell
que lo invocó. También puede iniciarse expresamente mediante
`eap.cmd terminal start --profile <profile>`.

Las pestañas activadas publican `core\commands` de forma explícita en `PATH`.
Esto permite ejecutar `eap`, `eap doctor` o cualquier otro subcomando sin conocer
la ubicación física del lanzador. El runtime `core\tools\python-embed` continúa fuera
de `PATH`. Las rutas de datos, configuración y workspace mostradas en el panel
son absolutas para que puedan copiarse directamente a CMD, PowerShell o Explorer.

Instalar, actualizar o restaurar un componente modifica el lock inmediatamente.
Las aplicaciones lanzadas después desde EAP y las pestañas nuevas reconstruyen
el profile actualizado. Las pestañas que ya estaban abiertas conservan, por
limitación del modelo de procesos de Windows, sus variables anteriores: basta
abrir otra con `+`; no es necesario reiniciar EAP.

Desactivar un componente lo elimina únicamente de la selección y del lock del
profile. Su payload compartido y sus datos se conservan. Los requisitos declarados
por los componentes son informativos: EAP no impone su orden de instalación ni
impide desactivarlos. Así, Maven puede usar un Java global del equipo aunque el
profile no tenga un Java gestionado por EAP. Resolver esas dependencias corresponde
al usuario del profile.

Desinstalar quita el componente del profile y elimina su payload de `components`
si ningún otro profile referencia esa instalación exacta. Si está compartido, EAP
lo conserva y muestra qué profiles siguen usándolo. Los datos personales bajo
`data/profiles` nunca se eliminan con esta operación. Los componentes externos sólo
pueden desvincularse: EAP no desinstala software perteneciente al equipo anfitrión.

`Catálogo de componentes > Activar componentes disponibles` enumera los payloads
válidos que existen bajo `components` pero no están activos en el profile actual.
La activación reconstruye su selección y lock directamente desde el marcador
`.eap-install.json`, sin consultar la red ni descargar archivos. Esto permite
reactivar un componente desactivado y aprovechar inmediatamente los payloads
incluidos en una exportación completa de EAP.

Los payloads nuevos conservan también su origen exacto de descarga. Al exportar
EAP con `components`, EAP añade esa metadata a la copia exportada usando los locks
locales existentes. Un payload antiguo sin origen conocido sigue pudiendo
activarse, pero la interfaz lo identifica como `sólo disponible localmente`: si
posteriormente se elimina, no podrá restaurarse exactamente desde el lock y habrá
que instalar o actualizar el componente desde el catálogo.

La pantalla principal muestra el tamaño y número de archivos de `temp`. La acción
`Opciones avanzadas > [4] Limpiar temporales` y `tool clean-temp` eliminan
descargas, staging,
transacciones y logs, pero se niegan a ejecutarse mientras hay otra operación EAP
activa.

Duplicar un profile copia su selección de componentes, lock y
`config.properties` privado. El nuevo profile crea y utiliza un workspace con su
propio nombre, pero conserva la referencia al mismo conjunto de datos. Esto permite
crear rápidamente, por ejemplo, `java11` desde un profile existente, mantener sus
preferencias y cambiar después sólo la línea de Java. Eliminar un profile borra su
definición, lock, estado y configuración privada, pero conserva workspace, datos y
payloads.

## Paquetes de profile y distribuciones de EAP

Son dos artefactos deliberadamente distintos:

- `profile export` crea `exports/envs/<nombre>.7z` para importarlo en otra copia
  de EAP. Contiene sólo el profile renombrado, su lock, su workspace y el manifiesto
  `eap-env-package.json`; no contiene `eap.cmd`, `core` ni el catálogo.
- `tool export` crea `exports/eap/<nombre>.7z`, una distribución arrancable de
  la herramienta. Contiene `eap.cmd`, `core`, catálogos/manifiestos y un
  `config.properties` general limpio; excluye `data`, `temp`, `exports`, `envs`
  y `workspaces` personales.

En un paquete de profile, `--include-components` añade únicamente los payloads
exactos fijados por su lock para poder importarlo sin descargarlos. Sin esa opción,
la EAP receptora puede recuperarlos mediante `profile restore` usando sus URL y hashes.

Los conjuntos de datos bajo `data` no se exportan. La frontera interna entre configuración,
caché, credenciales y estado vivo de aplicaciones como VS Code no es predecible
de forma genérica. Archivos como `.m2/settings.xml`, `.npmrc`, `.gitconfig`,
`.ssh` o `settings.json` deben compartirse manualmente y después de revisarlos.

`envs/<id>/config.properties` puede contener tokens. Por eso se sustituye por una
plantilla vacía al exportar, salvo que el usuario acepte expresamente
`--include-config`. El `config.properties` general nunca viaja en un paquete de
profile ni se copia tal cual en una distribución de EAP.

`profile import` valida rutas y límites del 7z, copia profile y workspace sin
sobrescribir destinos existentes, reutiliza payloads coincidentes y selecciona el
nuevo profile. Los antiguos comandos `env` y paquetes `eap-environment-export`
siguen siendo compatibles. Exportar e importar un profile concreto se agrupa bajo
`Gestionar profile`.

La acción principal `[0] Opciones avanzadas` contiene diagnóstico, limpieza de
temporales, actualización de EAP, la exportación completa de EAP, las operaciones
masivas e `Integraciones con el Host`. `Exportar todos los profiles` usa el
identificador de cada profile como nombre del paquete y no incluye payloads ni
`config.properties` privados. Si un archivo de destino ya existe, se informa del
error y se continúa con los demás sin sobrescribirlo.

`Importar todos los profiles` procesa, por orden, todos los `.7z` colocados
directamente en `envs`. Cada paquete se elimina únicamente después de importarse
correctamente; los que fallen permanecen en la bandeja y no detienen el resto del
lote. Los mismos flujos están disponibles mediante `profile export-all` y
`profile import-all`.

Durante una compresión interactiva, EAP conecta la salida de progreso nativa de
7-Zip a la terminal: se muestran el escaneo, el volumen de datos y el porcentaje
dinámico. En ejecuciones redirigidas, pruebas y scripts la salida continúa
capturada para no contaminar resultados automatizados.

La importación interactiva usa `envs` como bandeja de entrada: enumera los `.7z`
colocados directamente en esa carpeta y permite elegirlos por nombre. El archivo
seleccionado sólo se elimina cuando la importación ha finalizado correctamente;
si falla la validación o la extracción, permanece intacto para poder reintentarlo.
El comando `profile import <ruta>` continúa aceptando cualquier ruta explícita y no
elimina el archivo de origen, por lo que sigue siendo apropiado para scripts.

### Releases y actualización de EAP

`eap update --check` consulta la última release pública de
`danielgube/eap` sin autenticación. `eap update` instala la actualización después
de confirmarla y `eap update --yes` permite automatizarla. El mismo flujo está en
`Opciones avanzadas > [6] Actualizar EAP`.

El asset de release contiene exactamente los archivos versionados del tag salvo
`.gitignore`; `.git` tampoco forma parte del ZIP. Antes de instalar, EAP comprueba
el nombre, tamaño y SHA-256 publicado por GitHub, limita y valida la extracción y
verifica el contrato `core/release.json` y las dos declaraciones de versión. La
sustitución es transaccional: si falla, restaura el código anterior. Sólo se
reemplazan las rutas administradas; se conservan `core/tools`, `config.properties`,
`components`, `data`, `envs`, `exports`, `temp` y `workspaces`. Tras actualizar hay
que cerrar y volver a abrir EAP. Un checkout que contenga `.git` se protege de este
flujo y debe actualizarse con Git.

`eap release` es un comando administrativo deliberadamente ausente del menú. Se
ejecuta desde `main`, con el checkout limpio, `origin` apuntando al repositorio
oficial y el código sincronizado. Git Credential Manager realiza la autenticación
normal de GitHub —incluida la ventana del navegador cuando sea necesaria—; EAP no
pide ni guarda tokens. Antes de publicar comprueba que la cuenta tenga permiso de
escritura y ejecuta toda la suite de pruebas.

Si todavía no existe ninguna release, se usa la versión local. En las siguientes
se incrementa el parche de la última release completa. El comando actualiza las
dos declaraciones de versión, crea y sube el commit y el tag, genera
`exports/releases/eap-<versión>-windows-x64.zip` junto a su `.sha256`, crea la
release y sube ambos assets. Una ejecución interrumpida puede reanudar el commit,
tag, release o asset que haya quedado pendiente sin saltar de versión. Para crear
una release nueva después de modificar EAP, primero hay que hacer commit y push de
esos cambios y después ejecutar:

    eap release

Un profile importado sin payloads puede arrancar EAP y abrir shells en modo
degradado. Los componentes ausentes no publican sus variables ni sus entradas de
`PATH`; la interfaz los identifica y ofrece restaurarlos desde las versiones y URL
fijadas en el lock. La selección explícita del último profile, guardada en
`data/eap-state.json`, tiene prioridad sobre `profile.default`, que actúa sólo
como valor inicial o de reserva. `environment.default` continúa aceptándose como
alias de configuración para instalaciones anteriores.

## Workspace de trabajo

Cada profile guarda el identificador de su workspace en `environment.json`. La
asociación `default -> default`, por ejemplo, resuelve a
`C:\eap\workspaces\default`. Los shells CMD y PowerShell se abren en esa carpeta
y reciben `EAP_WORKSPACE` con su ruta absoluta. Cambiar la asociación crea la
nueva carpeta si hace falta, pero no mueve ni elimina el workspace anterior.

Los launchers declaran de forma obligatoria su política de trabajo:

- `environment`: terminales e IDEs, como VS Code, usan el workspace asociado;
- `component-data`: aplicaciones auxiliares, como DBeaver o un navegador, usan
  `data/profiles/<datos>/components/<componente>/workspace`.

Así, una aplicación de apoyo conserva su configuración, caché y espacio mutable
sin crear archivos propios dentro del proyecto. DBeaver es la primera aplicación
que utiliza este contrato de extremo a extremo.

## Datos portables del profile

Cada profile referencia sus datos bajo `data/profiles`. Al abrir un shell, EAP
redirige USERPROFILE, HOME, APPDATA, LOCALAPPDATA, TEMP, TMP y las rutas XDG a
ese conjunto de datos. ProgramFiles, SystemRoot y las demás ubicaciones del sistema no se
falsean.

De forma predeterminada, un profile nuevo crea datos con su mismo
identificador. La interfaz de creación permite elegir entre crear uno nuevo o
reutilizar datos existentes. Varios profiles pueden referenciar así el mismo
USERPROFILE portable y compartir `.m2`, `.gitconfig`, `.ssh`, preferencias y
cachés, aunque mantengan locks y workspaces distintos. La CLI ofrece la misma
relación mediante `profile create --data <datos>` y permite cambiarla con
`profile data <datos> --profile <profile>`.

Cambiar los datos sólo afecta a los procesos y pestañas que se abran después. EAP
crea el destino si no existe y no mueve ni elimina los datos de la asociación
anterior, porque éste puede seguir asociado a otros profiles.

### Integraciones con el Host

EAP mantiene portable el home completo salvo en integraciones que el usuario
active expresamente. La primera integración disponible es Firefox: crea un
*junction* desde
`data/profiles/<datos>/home/AppData/Roaming/Mozilla/Firefox` hacia el perfil real
de Firefox del usuario de Windows. De este modo, una autenticación externa abierta
por DBeaver, Kiro u otra aplicación hereda el navegador y las sesiones del host sin
romper el home único del profile para Maven, Git, npm, pip o Java.

La pantalla principal muestra únicamente las integraciones que el usuario haya
activado: `OK` cuando el enlace está sano y `KO` cuando continúa configurada pero
el enlace se ha roto o ya no apunta al origen esperado. Una integración nunca
activada, o desactivada expresamente, no ocupa espacio en el panel principal. Su
gestión está en `Opciones avanzadas > Integraciones con el Host`, que siempre
enumera las integraciones disponibles y muestra el origen, el destino, el conjunto
de datos afectado y todos los profiles que lo comparten. Firefox debe estar cerrado
para activar o desactivar la integración.

Si el destino EAP ya es un directorio normal, la activación no lo renombra ni crea
un backup: muestra su ruta, advierte que el borrado es permanente y exige una
confirmación explícita antes de eliminarlo y crear el junction. Si la operación no
termina correctamente, EAP restaura el directorio original. Desactivar sólo retira
el junction; nunca elimina el perfil Firefox del host.

`eap.cmd` captura el `USERPROFILE`, `APPDATA` y `LOCALAPPDATA` reales antes de
activar un profile y guarda ese contexto local en `data/host-context.json`. Estas
variables internas no se publican en shells ni launchers. Tanto `data` como el
contexto y los junctions quedan fuera de las exportaciones de EAP y de profiles.
La intención de activación se conserva junto al conjunto de datos en
`data/profiles/<datos>/host-integrations.json`; varios profiles que compartan esos
datos comparten también el mismo estado de integración.

Para Java también se fijan user.home y java.io.tmpdir. Maven utiliza
`data/profiles/<datos>/home/.m2`; EAP crea su repositorio y un `settings.xml`
mínimo si aún no existe. El archivo pertenece a esos datos y nunca se sobrescribe,
por lo que puede contener mirrors, proxies y servidores propios del profile.

Git publica `GIT_HOME` y antepone `cmd` al `PATH`. Su configuración global y
claves SSH quedan en `data/profiles/<datos>/home/.gitconfig` y `.ssh`, nunca en
el perfil real de Windows. EAP usa MinGit porque es el ZIP oficial pequeño y
automatizable; incluye la CLI de Git, pero no Git Bash ni Git GUI.

Node.js publica `NODE_HOME` y añade su raíz al `PATH`, donde viven `node.exe`,
`npm.cmd` y `npx.cmd`. La caché, el archivo de usuario y el prefijo global de npm
se redirigen respectivamente a `.npm`, `.npmrc` y `.npm-global` dentro del perfil.
La ruta `.npm-global` se añade también al `PATH`, por lo que los comandos
instalados con `npm install -g` permanecen portables y no modifican el payload.

El Python de `core/tools/python-embed` es exclusivamente el motor privado de EAP: no
se publica ni se utiliza para proyectos. El componente Python descarga el ZIP
completo `PythonCore` del índice oficial de Python Install Manager. Incluye
`venv`, `ensurepip` y pip; EAP genera un `pip.cmd` portable y mantiene paquetes,
scripts, configuración y caché bajo los datos del profile. Un `pip install` normal
queda en `data/profiles/<datos>/home/.python`, sin modificar el runtime.

`uv` no se mezcla con el payload de Python: es una herramienta con ciclo de
actualización propio y debe incorporarse como un componente independiente.

### Contrato genérico de datos

Los manifiestos pueden declarar `data.directories` y `data.files`. Todas las
rutas deben resolverse dentro de `data/profiles/<datos>`. Los archivos usan el
modo `if-missing`: EAP genera una semilla inicial, pero conserva cualquier cambio
posterior del usuario.

`{{profile.home}}` es el único home de usuario dentro de un profile y mantiene
la coherencia entre herramientas como Maven, Git, npm, pip y las aplicaciones
que estas lancen. Ningún launcher puede redefinir `USERPROFILE`, `HOME`,
`APPDATA`, `LOCALAPPDATA`, los temporales, las rutas XDG ni
`JAVA_TOOL_OPTIONS`. `{{data.component}}` aísla aplicaciones únicamente mediante
rutas explícitas, como el runtime de DBeaver y los datos de VS Code y VSCodium.
La carpeta
`data/profiles/<datos>/components/<id>` sólo se crea cuando el manifiesto, un
launcher o un comando generado la necesita; no es obligatoria para todos los
componentes.

### Bruno

Bruno se instala desde el ZIP portable oficial de Windows x64 en
`components/bruno/community/<versión>`. EAP sigue la línea estable 4.x,
selecciona únicamente releases finales y verifica el SHA-256 publicado con el
artefacto de GitHub.

El launcher abre Bruno con el workspace del profile como directorio de trabajo
y pasa `--user-data-dir` para mantener preferencias, cachés y estado interno en
`data/profiles/<datos>/components/bruno/user-data`. Las colecciones y workspaces
son archivos locales compatibles con Git; su ubicación predeterminada queda en
`data/profiles/<datos>/home/Documents/bruno`, dentro del home privado del
profile. Pueden guardarse también junto al proyecto desde la propia interfaz.

La aplicación de escritorio permite crear y consumir APIs HTTP, GraphQL, gRPC,
WebSocket y cURL. El CLI `bru`, orientado a automatización y CI, es un paquete de
Node.js independiente y no forma parte de este componente gráfico.

### DBeaver Community

DBeaver se instala side-by-side en
`components/dbeaver/community/<versión>` y no añade rutas ni variables al shell.
La distribución incluye su propio OpenJDK, por lo que no depende del Java activo
del profile.

Al arrancarlo, EAP crea un proceso GUI separado con estas ubicaciones:

- workspace: `data/profiles/<datos>/components/dbeaver/workspace`;
- configuración de Eclipse y p2:
  `data/profiles/<datos>/components/dbeaver/runtime/<versión>`.

El argumento oficial `-data` fija el workspace de conexiones, scripts y
preferencias. DBeaver hereda el home único del profile, por lo que sus drivers y
ajustes quedan en `home/AppData/Roaming/DBeaverData` y comparte de forma coherente
la identidad portable, la configuración de herramientas y las integraciones del
sistema con el resto de componentes. Estos datos nunca llegan al perfil real de
Windows. EAP copia una semilla versionada de `configuration` y `p2` antes del
primer arranque para conservar inmutable el payload compartido.

Desde `Catálogo de componentes > DBeaver Community` se puede actualizar, cambiar
su selección, ejecutar la aplicación o crear un acceso directo en el escritorio.
El `.lnk` usa el icono de
DBeaver con una etiqueta visual `EAP`, pero apunta al `pythonw.exe` privado con
`eap launch dbeaver --profile <id>`:
no abre una consola ni omite la activación portable. Es un artefacto local y
regenerable; no se exporta con EAP ni con el profile.

Los accesos directos conservan el icono multirresolución de cada aplicación y
añaden una etiqueta naranja `EAP` en la esquina inferior derecha. Los iconos
compuestos se guardan de forma persistente bajo
`data/shortcut-icons/<profile>` para que Windows no pierda la referencia. El
runtime privado incluye Pillow exclusivamente para esta composición; la
librería no se publica en los shells ni queda disponible para los proyectos.

### Visual Studio Code y VSCodium

Visual Studio Code se instala en `components/vscode/microsoft/<versión>`.
VSCodium aparece como alternativa open source independiente en
`components/vscodium/community/<versión>`. Ambos launchers usan la política
`environment`: abren directamente el workspace asociado al profile activo y
heredan sus runtimes, herramientas y variables.

EAP no crea la carpeta `data` dentro del payload del editor. En su lugar pasa
`--user-data-dir` y `--extensions-dir` para guardar preferencias, cachés,
credenciales y extensiones bajo
`data/profiles/<datos>/components/<editor>`. También desactiva la actualización
interna del editor para que el ciclo de versión quede gobernado por el catálogo,
el lock y la política `same-track` de EAP.

El comando `code` queda disponible en los shells que tengan Visual Studio Code
activo; `codium` hace lo mismo para VSCodium. Un acceso directo creado desde la
interfaz sigue apuntando al launcher estable de EAP, de modo que no pierde el
workspace ni la activación portable del profile.

### IntelliJ IDEA

Desde la versión 2025.3, JetBrains distribuye IntelliJ IDEA como un producto
unificado: el mismo ZIP ofrece gratuitamente las capacidades que antes
correspondían a Community y desbloquea las funciones Ultimate cuando el usuario
activa una licencia o suscripción. Por eso EAP publica un único componente
`intellij-idea`; no duplica el payload bajo nombres Community y Enterprise que ya
no corresponden a distribuciones distintas.

EAP resuelve el ZIP portable de Windows x64 y su SHA-256 desde la API oficial de
JetBrains. Se instala en
`components/intellij-idea/jetbrains/<versión>` y abre directamente el workspace
del profile. La línea `same-track` permite conservar una versión compatible con
una licencia perpetua anterior o seguir la línea estable actual.

El launcher define `IDEA_PROPERTIES` sin modificar el payload. Configuración,
cachés, plugins y logs quedan separados bajo
`data/profiles/<datos>/components/intellij-idea`; el archivo generado
`idea.properties` fija sus cuatro rutas portables. Tanto el modo gratuito como
Ultimate comparten este aislamiento y el home privado del profile.

### Eclipse IDE

Eclipse se ofrece como una familia con dos paquetes oficiales de Eclipse
Packaging Project: `java`, que instala Eclipse IDE for Java Developers, y
`enterprise-java`, que añade las herramientas para Enterprise Java y desarrollo
web. Ambos paquetes incluyen su propio JRE y se verifican con el SHA-512
publicado por Eclipse Foundation.

Los payloads se instalan side-by-side en
`components/eclipse/<proveedor>/<versión>`. El launcher abre el workspace del
profile mediante `-data` y copia `configuration` y `p2` a
`data/profiles/<datos>/components/eclipse/runtime/<proveedor>/<versión>` antes
del primer arranque. Así, los datos de Equinox de Java y Enterprise no se
mezclan ni modifican sus plantillas originales. `ECLIPSE_HOME` y el comando
`eclipse` quedan disponibles únicamente en los profiles que tengan activo el
componente.

## Pocketools

Las Pocketools son utilidades globales pequeñas que no pertenecen a un profile
ni al core de EAP. Se publican en repositorios independientes, se instalan bajo
`pocketools/packages` y EAP genera sus comandos en `pocketools/bin`. Ese único
directorio se añade al `PATH` después de las rutas de los componentes activos y
antes de las herramientas core.

El repositorio público predeterminado es
`https://github.com/danielgube/eap-pocketools`. Se pueden añadir repositorios
GitHub públicos de otras organizaciones:

    eap.cmd pocketool repository add empresa https://github.com/empresa/eap-pocketools
    eap.cmd pocketool refresh
    eap.cmd pocketool search zip
    eap.cmd pocketool install empresa/zipme
    eap.cmd pocketool update empresa/zipme
    eap.cmd pocketool uninstall empresa/zipme

EAP consulta el árbol `main`, descubre automáticamente los manifiestos bajo
`pocketools/*/pocketool.json` y fija cada instalación al commit consultado. Los
archivos se descargan directamente desde ese commit y se contrastan con su
tamaño e identificador de objeto Git. No hacen falta catálogos generados,
GitHub Releases, tags ni ZIPs.

Cada instalación rechaza rutas inseguras y colisiones de comandos, y publica el
lock y los shims de forma transaccional.
Las dependencias entre Pocketools se resuelven como grafo y los ciclos se
rechazan. Las dependencias de componentes se evalúan contra el profile activo al
instalar y cada vez que se lanza el comando.

La primera Pocketool es `sessionkeep`, con los comandos `start`, `stop`,
`status` y `--help`. Mantiene una única instancia oculta y conserva su estado en
`data/pocketools/state`, fuera del payload versionado.

Para publicar una actualización basta con cambiar el fuente, incrementar la
versión de `pocketool.json`, probar y hacer push a `main`. Las exportaciones de
EAP y de profiles no incluyen Pocketools ni sus datos; se restauran desde sus
repositorios públicos.

## Estructura

- core/tools: payloads ejecutables administrados por el bootstrap;
- core/tools/python-embed: runtime privado mínimo;
- core/tools/7zip: herramienta de compresión y transferencia;
- core/tools/mkcert: certificados locales de desarrollo;
- core/tools/openssl: CLI y librerías criptográficas portables;
- core/tools/ripgrep: búsqueda recursiva rápida;
- core/tools/windows-terminal: Windows Terminal portable administrado;
- core/bootstrap.ps1: reconstruccion transaccional de payloads core ausentes;
- core/core_tools.json: herramientas core, descargas, hashes e instalacion;
- core/app/eap: motor de EAP;
- core/app/eap/shortcuts.py: generación controlada de accesos directos Windows;
- core/catalog/host-integrations.json: catálogo de integraciones explícitas con el host;
- core/catalog.json: catálogo general;
- components/*_eap_component.json: contratos de familias;
- components/java/<proveedor>/<version>: instalaciones compartidas;
- components/maven/apache/<version>: instalaciones Maven compartidas;
- components/git/git-for-windows/<version>: instalaciones MinGit compartidas;
- components/nodejs/nodejs/<version>: instalaciones Node.js compartidas;
- components/python/pythoncore/<version>: instalaciones CPython compartidas;
- components/bruno/community/<version>: instalaciones Bruno compartidas;
- components/dbeaver/community/<version>: instalaciones DBeaver compartidas;
- components/vscode/microsoft/<version>: instalaciones VS Code compartidas;
- components/vscodium/community/<version>: instalaciones VSCodium compartidas;
- components/eclipse/<proveedor>/<version>: instalaciones Eclipse IDE compartidas;
- components/intellij-idea/jetbrains/<version>: instalaciones IntelliJ IDEA compartidas;
- pocketools/bin: shims globales generados por EAP;
- pocketools/packages/<repositorio>/<id>/<version>: payloads Pocketool;
- data/pocketools: lock, índices Git guardados y estado mutable de Pocketools;
- envs/<nombre>: definición persistida del profile, lock, estado y
  `config.properties` privado; `envs` conserva su nombre físico por compatibilidad;
- data/profiles/<nombre>: conjuntos de datos compartibles entre profiles,
  incluido el USERPROFILE portable y los datos mutables de aplicaciones;
- exports: paquetes 7z generados para compartir;
- temp: descargas, staging, transacciones y logs desechables;
- workspaces/<nombre>: proyectos asociados a los profiles.

## Configuración

`config.properties` contiene ajustes generales de EAP. Cada profile dispone de
`envs/<id>/config.properties`; sus valores tienen prioridad para ese profile. Las
propiedades con forma `env.NOMBRE=valor` se publican como variables `NOMBRE` en
shells y launchers. Por ejemplo:

    # config.properties general
    env.COMPANY_API_URL=https://api.example
    pocketools.repository.danielgube=https://github.com/danielgube/eap-pocketools
    pocketools.repository.empresa=https://github.com/empresa/eap-pocketools

    # envs/hbx/config.properties
    env.COMPANY_API_TOKEN=valor-privado

EAP impide sobrescribir con este mecanismo `PATH`, las rutas del perfil portable,
las variables `EAP_*` y las variables gestionadas por componentes. Los valores no
se escriben en locks, manifiestos ni diagnósticos. `config.properties.example`
documenta las opciones generales sin contener secretos.

## Pruebas

    core\tools\python-embed\python.exe -B -I -X utf8 -m unittest discover -s core\tests -v

El diseño completo, las decisiones y las siguientes fases están en sdd_plan.
