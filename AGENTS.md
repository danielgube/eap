# Guía de trabajo para EAP

Este archivo se aplica a todo el repositorio. Su objetivo es que una persona o
un agente pueda continuar EAP sin depender del historial de conversaciones de
un ordenador concreto.

## Antes de modificar código

1. Lea `spec/README.md` y después el documento de `spec/` relacionado con la
   tarea.
2. Lea `README.md` para conocer la experiencia pública vigente.
3. Compruebe `git status` y preserve cualquier cambio que no sea suyo.
4. Trate el código y las pruebas como fuente de verdad si un documento histórico
   contradice la implementación actual.

## Invariantes del producto

- EAP es Windows x64, portable y autocontenido. No debe requerir instalaciones
  globales ni modificar permanentemente el registro o el `PATH` del host.
- Todo estado administrado vive bajo la raíz de EAP. Las variables se inyectan
  sólo en procesos hijos.
- Un profile combina componentes activos, un workspace, un conjunto de datos y
  configuración privada. Workspace y datos son ejes independientes y pueden
  compartirse o cambiarse sin reinstalar componentes.
- Los payloads de componentes son inmutables, compartidos entre profiles y
  reconstruibles. La selección exacta queda fijada en el lock del profile.
- Existe un único home por conjunto de datos. Ningún componente puede redefinir
  `USERPROFILE`, `HOME`, `APPDATA`, `LOCALAPPDATA`, temporales o variables
  reservadas equivalentes.
- Las dependencias entre componentes son informativas: el usuario puede apoyarse
  en software global. Las dependencias declaradas por Pocketools sí se validan
  contra el profile desde el que se ejecutan.
- Los catálogos son declarativos y externos. No se debe acoplar el core a IDs,
  URLs o productos concretos salvo que una capacidad sea genuinamente general.
- No se importa ni ejecuta Python remoto desde un catálogo. Una futura API de
  adaptadores debe ser versionada, aislada y conservar en el core la descarga,
  verificación, extracción y publicación.
- Nunca se desactiva la validación TLS para resolver problemas corporativos. La
  confianza de Windows y los proxies se integran de forma explícita y auditable.
- Descargas, extracción, importación, actualización y publicación usan staging,
  validación, límites, rutas confinadas y commit atómico o rollback.
- El workspace y los datos personales no viajan en una exportación de profile.
  Payloads, configuración privada y `custom-commands` sólo se incluyen mediante
  opciones explícitas.
- No se escriben secretos en logs, locks, diagnósticos, documentación o Git.

## Interfaz y compatibilidad

- La interfaz interactiva funciona como páginas: limpia la pantalla, muestra
  breadcrumb, usa paneles de texto y pausa tras resultados o errores.
- Los accesos seleccionables se muestran en amarillo; títulos y marcos en cian;
  `OK` en verde; `KO` y errores en rojo. El color se aplica después de calcular
  el layout y se desactiva si la salida no es una terminal.
- Mantenga `profile` y `--profile` como nomenclatura pública. `env` y `--env`
  siguen siendo aliases compatibles hasta que exista una migración explícita.
- Una aplicación debe recibir el workspace como argumento cuando no baste con el
  directorio de trabajo. Los accesos directos llaman a EAP, no fijan rutas
  internas del payload.

## Flujo de implementación

- Prefiera ampliar contratos declarativos y reutilizar primitivas existentes.
- Añada una primitiva Python sólo cuando varios productos puedan beneficiarse de
  ella y documente el contrato en el repositorio de catálogo correspondiente.
- Mantenga escrituras atómicas y valide siempre que una ruta permanezca dentro de
  su raíz permitida. Considere nombres reservados, rutas largas, archivos de sólo
  lectura y procesos externos que eliminen o bloqueen archivos.
- Añada una prueba de regresión por cada fallo corregido.
- Después de modificar código, ejecute:

  ```powershell
  .\core\tools\python-embed\python.exe -B -I -X utf8 -m unittest discover -s core\tests -p test_*.py
  git diff --check
  .\eap.cmd doctor
  ```

- `doctor` puede depender del estado local; explique cualquier aviso legítimo.
- Para cambios de manifiestos, ejecute además el validador del repositorio
  `eap-components` o `eap-pocketools` y pruebe refresh, resolución e instalación.
- No cree una release como parte de una tarea ordinaria. `eap release` publica en
  GitHub, crea commit y tag, y requiere autorización explícita del usuario.

## Repositorios relacionados

- Core: `https://github.com/danielgube/eap`
- Componentes: `https://github.com/danielgube/eap-components`
- Pocketools: `https://github.com/danielgube/eap-pocketools`

Los contratos públicos detallados de componentes y Pocketools viven en los dos
repositorios externos. No duplique aquí sus manuales completos.
