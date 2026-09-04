# Producto EAP

## Propósito

EAP convierte una carpeta escribible de Windows en un puesto de desarrollo
portable y reproducible. Permite cambiar entre proyectos y stacks sin instalar
herramientas globalmente, sin depender del `PATH` del sistema y sin mezclar los
datos personales de todos los proyectos.

La promesa principal es:

> Tu entorno de desarrollo cabe en una carpeta y puede reconstruirse a partir de
> estado declarativo y artefactos verificables.

## Usuarios y situaciones principales

- Una persona que trabaja con varias versiones de Java, Node, Python u otros
  runtimes en el mismo ordenador.
- Equipos que necesitan preparar puestos reproducibles en Windows.
- Entornos corporativos con proxy, inspección TLS, acceso parcial a Internet o
  necesidad de mover código y herramientas entre redes.
- Personas que quieren llevar aplicaciones, configuración y utilidades sin
  contaminar el host.

EAP debe seguir siendo útil tanto en un equipo personal como en un equipo
corporativo restringido. La experiencia básica no puede depender de que Python,
Git, Java, Node o Windows Terminal ya estén instalados.

## Modelo mental

### Component

Software grande o con ciclo de versión propio: runtime, herramienta de build,
servidor o aplicación. EAP resuelve un artefacto, lo verifica, lo instala como
payload inmutable y lo activa en uno o varios profiles.

Un Component tiene dos clasificaciones diferentes:

- `kind`: naturaleza técnica cerrada (`runtime`, `tool`, `server`, `service`,
  `application` o `external`).
- `category`: agrupación visual, por ejemplo runtimes, clientes de bases de datos
  o IDEs.

Un cliente gráfico de base de datos es `kind: application` y puede pertenecer a
`category: database-clients`. Categoría y tipo nunca son sinónimos.

### Pocketool

Utilidad pequeña, global a la instalación de EAP y orientada a comandos. Publica
shims en un único directorio del `PATH`. Su código es inmutable; su estado se
guarda fuera del paquete. Puede exigir Pocketools o capacidades de Components
presentes en el profile desde el que se ejecuta.

Examples públicos actuales: mantener una sesión activa, empaquetar sólo el
fuente de un proyecto y diagnosticar confianza TLS.

### Profile

Selección reproducible para trabajar. Contiene:

- Components activos y sus versiones exactas;
- un workspace asociado;
- un conjunto de datos asociado;
- configuración privada;
- estado operativo y lock.

El nombre público es **profile**. La carpeta física continúa llamándose `envs/`
por compatibilidad, y `env` permanece como alias de CLI.

### Workspace

Código o documentos sobre los que se trabaja. Es independiente del runtime y de
los datos. Dos profiles pueden apuntar al mismo workspace, aunque normalmente se
usa uno por proyecto.

El workspace nunca se incluye automáticamente al exportar un profile.

### Datos

Home portable compartible entre profiles: `.m2`, `.npm`, configuración de IDE,
AppData, cachés, claves locales y otros estados de usuario. Un profile Java 11 y
otro Java 21 pueden compartir los mismos datos y usar workspaces distintos.

Compartir datos implica compartir todo el home, no sólo Maven o Git. EAP no
simula aislamiento parcial por aplicación.

## Experiencia principal

1. El usuario descarga una release o clona el repositorio.
2. `eap.cmd` detecta las herramientas internas ausentes y pide permiso para
   reconstruirlas.
3. Se crea o selecciona un profile.
4. Se actualizan los catálogos configurados.
5. El usuario instala o activa Components y Pocketools.
6. EAP abre un terminal o una aplicación con el entorno del profile.
7. El estado exacto queda fijado para poder restaurarlo o transportarlo.

La interfaz interactiva debe ser suficiente para descubrir el producto. La CLI
debe ofrecer los mismos flujos para automatización, además de operaciones
administrativas como `release`.

## Principios de producto

- **Portable de verdad:** no depender del estado global del host salvo cuando el
  usuario elige una integración explícita.
- **Reproducible:** un lock identifica proveedor, track, versión, origen y huella.
- **Declarativo:** añadir un producto normal debe ser un cambio de manifiesto, no
  una dispersión de condicionales por el core.
- **Seguro por defecto:** TLS validado, rutas confinadas, límites de tamaño y
  publicación transaccional.
- **Recuperable:** un fallo no debe convertir una descarga parcial en una
  instalación válida ni impedir abrir EAP en modo degradado.
- **Comprensible:** la interfaz muestra procedencia, versión, tipo, categoría,
  actualización disponible y rutas importantes.
- **Responsabilidad del usuario:** EAP informa de dependencias entre Components,
  pero no impide combinaciones que puedan apoyarse en herramientas del host.

## Exportación

Una exportación de profile transporta el estado declarativo y el lock. Puede
incluir explícitamente:

- payloads exactos;
- `config.properties` privado;
- `custom-commands`.

No incluye los datos personales ni el workspace. Esta exclusión es intencional:
archivos como `.m2/settings.xml`, `.npmrc`, `.gitconfig`, `.ssh` o la configuración
de un IDE pueden contener secretos y tienen una semántica difícil de predecir.
Si deben compartirse, se revisan y copian manualmente.

## Fuera de alcance actual

- Sistemas operativos distintos de Windows x64.
- Instaladores MSI/EXE que modifiquen el sistema.
- Gestión de paquetes del sistema, servicios Windows o elevación automática.
- Sincronización automática de workspaces o datos personales.
- Ejecución de código remoto arbitrario desde catálogos.
- Desactivar validación TLS para sortear problemas de certificados.
- Resolver por el usuario qué dependencias de Components son obligatorias.
