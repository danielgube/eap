# Backlog y riesgos conocidos

Esta lista no sustituye a Issues. Recoge trabajo transversal que se perdería con
facilidad al cambiar de ordenador. Antes de implementarlo, confirme que sigue
vigente y conviértalo en una tarea con criterios de aceptación.

## Prioridad alta

### Definir la salida a 1.0.0

Las conversaciones históricas mencionan 1.0.0, pero no existe una checklist
formal y cerrada. Definir explícitamente:

- contratos que se consideran estables;
- compatibilidad prometida para manifests, locks y paquetes;
- escenarios de primera instalación, actualización y recuperación;
- soporte esperado en redes corporativas y sin conexión;
- documentación y pruebas de aceptación de los tres repositorios.

No incrementar a 1.0.0 sólo porque se hayan completado antiguas “últimas dos
funciones”.

### Probar releases independientemente del profile activo

Históricamente, ejecutar `eap release` desde un terminal EAP heredó `JAVA_HOME` y
alteró pruebas de profiles degradados. La práctica segura es publicar desde una
terminal normal. Falta convertir la independencia del entorno heredado en una
garantía probada del comando de release.

### Mantener contratos sincronizados entre repositorios

Añadir una validación cruzada automatizada que compruebe el catálogo oficial de
Components y las Pocketools contra el core actual. Hoy los repositorios tienen
validadores propios, pero una evolución del core puede dejar documentación o
manifiestos incompatibles sin detectarlo en la misma ejecución.

## Evolución arquitectónica

### Adapter API v1 aislada

Objetivo: permitir resolución, validación o comparación específicas sin modificar
el core por producto. Requisitos antes de aceptar código externo:

- protocolo versionado de entrada/salida;
- proceso separado con timeout, límites de tamaño y entorno mínimo;
- sin acceso a secretos del proxy o configuración privada;
- red mediada por el core o explícitamente prohibida;
- artefactos y resultados revalidados por el core;
- modelo de confianza, firma y actualización del adaptador;
- compatibilidad y diagnóstico claros.

Hasta entonces, no importar Python de un catálogo.

### Locks huérfanos

`FileLock` impide operaciones concurrentes y elimina el archivo en salidas
controladas. Una terminación abrupta del proceso principal puede dejarlo. Evaluar
una recuperación segura que compruebe PID, instante y pertenencia a la operación
antes de retirarlo; nunca borrar locks activos por antigüedad solamente.

### Autenticación y límites de GitHub

Los refresh públicos pueden alcanzar el rate limit de GitHub sin autenticar.
Diseñar una mejora sólo si se mantiene la regla de no filtrar ni persistir tokens
sin consentimiento. El modo offline debe seguir usando el último snapshot válido.

## Deuda documental conocida

- El README observado en `eap-components` todavía afirmaba que EAP mantiene un
  snapshot integrado de bootstrap. El core 0.19.11 ya usa un catálogo base vacío;
  al próximo cambio del catálogo debe corregirse esa frase.
- Mantener las capturas del README fuera del asset de release. `spec/` y
  `AGENTS.md` sí están declarados como rutas administradas.
- Si cambia `kind`, `category`, `info` o cualquier resolver, actualizar en la
  misma tarea el contrato de `CREAR_COMPONENTES.md`.

## Riesgos aceptados

- `html-links` puede trabajar sin checksum publicado. EAP calcula una huella
  local para reproducibilidad, pero eso no prueba la autenticidad del origen; en
  HTTP tampoco protege el tránsito.
- Java con `trust.windows=true` usa el almacén `Windows-ROOT`; no es una mezcla
  automática con el `cacerts` distribuido por cada JDK.
- Compartir un conjunto de datos entre profiles comparte también sesiones,
  cachés y configuración de IDE, no sólo repositorios de dependencias.
- Un Component `external` depende de una ruta del host y por definición no es tan
  transportable como un payload administrado.

## No reabrir sin un requisito nuevo

- Exportar automáticamente `.m2/settings.xml`, `.npmrc`, `.ssh` o configuración
  de IDE: se retiró deliberadamente por privacidad e imprevisibilidad.
- Proteger o resolver automáticamente dependencias entre Components: pertenecen
  al usuario y pueden satisfacerse desde el host.
- Permitir junctions arbitrarias desde AppData: las integraciones siguen una
  whitelist versionada.
- Desactivar TLS para solucionar PKIX, proxy o inspección corporativa.
