# Decisiones de diseño

Este registro contiene decisiones vigentes y algunas decisiones sustituidas que
son fáciles de reintroducir por accidente.

## D-001 — Portabilidad confinada a una raíz

**Estado:** vigente.

EAP administra todo bajo su propia carpeta e inyecta configuración sólo a
procesos hijos. No modifica de forma permanente el `PATH`, el registro ni el home
real del usuario. Esta frontera permite copiar, borrar o mantener varias
instalaciones sin contaminar Windows.

## D-002 — Profile, workspace y datos son ejes distintos

**Estado:** vigente.

Un profile selecciona versiones y asocia un workspace y un conjunto de datos.
Cambiar de Java 11 a Java 21 no debe obligar a duplicar `.m2`, Git o la
configuración de usuario. Compartir datos es explícito y puede afectar a todas
las aplicaciones que usen ese mismo home.

La carpeta física `envs/` se conserva para evitar una migración destructiva; el
lenguaje público es `profile`.

## D-003 — Un único home por conjunto de datos

**Estado:** vigente y considerado invariante fuerte.

Los Components heredan el home del profile. No pueden crear otro `USERPROFILE` o
sobrescribir AppData y temporales reservados. El problema histórico de DBeaver,
que creó un home anidado propio, motivó la validación actual.

## D-004 — Payloads inmutables y compartidos

**Estado:** vigente.

`components/` contiene instalaciones por proveedor y versión que pueden usar
varios profiles. Desactivar no borra; desinstalar sólo borra si ya no hay usuarios.
El lock, no el contenido mutable de un profile, identifica el payload exacto.

## D-005 — Las dependencias de Components son informativas

**Estado:** vigente.

EAP no bloquea Maven porque Java no esté activo ni fuerza grafos de instalación.
El usuario puede utilizar un Java global o una combinación deliberada. En cambio,
las Pocketools sí declaran requisitos ejecutables y EAP los valida al instalar y
ejecutar, porque sus shims se publican globalmente.

## D-006 — Catálogo de Components externo y multifuente

**Estado:** vigente; sustituye el catálogo integrado.

Las URLs iniciales viven en `config.properties.example`. El motor sólo entiende
el patrón `components.repository.<id>` y puede combinar repositorios. Cada fuente
mantiene identidad y revisión; las colisiones se rechazan.

**Decisión sustituida:** el catálogo integrado se conservó temporalmente como
bootstrap y respaldo offline. Desde el desacoplamiento completo, el
`core/catalog/catalog.json` está vacío y la disponibilidad offline procede de
snapshots externos ya validados.

## D-007 — Core genérico, sin Python remoto importado

**Estado:** vigente.

El objetivo no es congelar el core para siempre, sino evitar lógica dispersa por
producto. Los manifiestos usan resolvers y validadores publicados. Si falta una
capacidad general, se añade al core y al contrato.

No se cargan `.py` de terceros en el proceso de EAP: tendrían acceso a secretos,
red, filesystem y estado global. La futura Adapter API v1 debe ejecutar fuera del
proceso, con protocolo limitado, versión, límites y aislamiento. El core seguirá
siendo dueño de descarga, verificación, extracción y commit.

## D-008 — Pocketools es un subsistema distinto

**Estado:** vigente.

Una Pocketool es global y publica comandos; un Component se activa por profile y
publica runtime, aplicaciones o variables. Mezclarlos produciría semántica
confusa. Pocketools se instala directamente desde archivos fijados a un commit de
`main`, sin releases, tags ni ZIPs generados.

## D-009 — Exportación conservadora

**Estado:** vigente.

Se probó exportar selectivamente configuración declarada de aplicaciones, pero se
retiró: rutas anidadas, secretos y semántica específica hacían el resultado
impredecible. Los datos y el workspace nunca se exportan. Configuración privada,
payloads y `custom-commands` requieren consentimiento explícito.

## D-010 — Seguridad TLS sin atajos inseguros

**Estado:** vigente.

No se usa `NODE_TLS_REJECT_UNAUTHORIZED=0`, `strict-ssl=false` ni equivalentes.
La solución corporativa es integrar proxy y almacén de confianza de Windows por
profile. Las URLs de diagnóstico no limitan el alcance de una CA: son pruebas,
no certificados instalables.

## D-011 — Integraciones con el host mediante whitelist

**Estado:** vigente.

EAP no ofrece un explorador genérico para enlazar AppData. Sólo permite recetas
versionadas y revisables. Firefox enlaza Roaming, no la caché Local. Si hay datos
portables previos, se ofrece borrarlos con confirmación; desactivar nunca borra el
origen del host.

## D-012 — Instalación y recuperación transaccionales

**Estado:** vigente.

Una operación sólo se hace visible tras validar completamente el resultado. Los
fallos conservan un artefacto reutilizable cuando es seguro, eliminan staging y
no actualizan el lock. La extracción se aisló en 7-Zip porque se observó que un
antivirus podía eliminar un JAR y terminar un proceso nativo. EAP verifica después
la lista y el tamaño de todas las entradas.

Los locks evitan concurrencia; antes de eliminar manualmente un lock tras una
caída se debe confirmar que el PID registrado ya no existe.

## D-013 — Logs simples de toda la consola

**Estado:** vigente.

Un archivo por ejecución captura `stdout`, `stderr` y excepciones, hace flush de
cada escritura y elimina color ANSI. La ausencia de la marca final ayuda a
detectar una terminación externa. No se añadió una dependencia de logging.

La limpieza de temporales también limpia logs antiguos y reinicia el log activo
sin dejar de capturar la sesión.

## D-014 — Interfaz de páginas, no TUI dinámica

**Estado:** vigente.

La UI imprime paneles completos, no mueve el cursor ni repinta líneas antiguas.
Cada navegación limpia la pantalla, muestra breadcrumb y pausa tras resultados y
errores. Esto prioriza robustez en terminales de anchuras distintas. Los códigos
ANSI se aplican después del cálculo del ancho para que no desplacen bordes.

## D-015 — Launchers y accesos directos estables

**Estado:** vigente.

Un acceso `.lnk` llama al launcher estable de EAP con profile e ID; no apunta al
payload versionado. Así una actualización de manifiesto se aplica sin recrear el
acceso. El launcher debe pasar explícitamente el workspace si la aplicación, como
Kiro, restaura su última ventana cuando sólo cambia el directorio de trabajo.

Los iconos derivados se guardan en datos y añaden una insignia EAP sin modificar
el icono original.

## D-016 — Bootstrap declarativo del propio core

**Estado:** vigente.

Git contiene código y manifiestos, no binarios internos. `core/core_tools.json`
fija URL, versión, hash, pasos y validación. `bootstrap.ps1` reconstruye el core
en el primer arranque y en rutas largas, con staging y publicación atómica.

## D-017 — Actualización pública y release administrativa

**Estado:** vigente.

`eap update` sólo reemplaza rutas administradas y preserva estado local. Verifica
el digest del asset y puede hacer rollback. `eap release` es una operación de
autor, no una función protegida por ocultación: Git/GitHub deben confirmar
repositorio, estado, identidad y permisos. El asset se construye desde el tag.

`docs/images` se excluye del ZIP público porque sólo sirve al README de GitHub.
La especificación y `AGENTS.md` sí forman parte del contrato administrado.

## D-018 — Evolución compatible del vocabulario

**Estado:** vigente.

Se migró de “entorno” a “profile” para reducir la ambigüedad con el entorno de
procesos y con los datos. Los comandos `env` y `--env`, así como `EAP_ENV`, se
mantienen por compatibilidad. No deben retirarse sin versión mayor y migración.
