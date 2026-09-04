# Especificación y continuidad de EAP

Esta carpeta conserva el contexto duradero del proyecto. Existe para que EAP se
pueda retomar después de cambiar de ordenador sin depender de chats, memorias de
Codex, instalaciones locales o conocimiento tácito.

## Orden de lectura

1. [`PRODUCT.md`](PRODUCT.md): propósito, modelo mental y límites del producto.
2. [`ARCHITECTURE.md`](ARCHITECTURE.md): estructura, flujos y responsabilidades.
3. [`DECISIONS.md`](DECISIONS.md): decisiones que no deben redescubrirse.
4. [`CONTINUITY.md`](CONTINUITY.md): estado verificable, evolución y reanudación.
5. [`BACKLOG.md`](BACKLOG.md): trabajo pendiente y riesgos conocidos.

`AGENTS.md`, en la raíz, resume las reglas operativas que deben cargarse antes de
cualquier tarea.

## Fuentes de verdad

Cuando dos fuentes discrepen, use este orden:

1. Código ejecutado y pruebas de la revisión actual.
2. Contratos JSON y validadores actuales.
3. `spec/DECISIONS.md` y `spec/ARCHITECTURE.md`.
4. `README.md` y manuales de los repositorios externos.
5. `spec/CONTINUITY.md`, commits y conversaciones históricas.

Una conversación describe el estado de su momento. No debe revivir una decisión
que ya haya sido sustituida. Ejemplo importante: durante un tiempo existió un
catálogo de componentes integrado como respaldo; el core actual conserva sólo
un catálogo vacío de contrato y obtiene los productos de fuentes externas.

## Alcance de esta documentación

Aquí se documenta:

- la intención del producto y su vocabulario;
- las fronteras entre core, catálogos, profiles, datos y payloads;
- las garantías de portabilidad, seguridad y recuperación;
- las decisiones históricas con consecuencias para cambios futuros;
- el estado que debe comprobarse al clonar en un equipo nuevo.

Aquí no se guardan:

- tokens, contraseñas, proxies privados ni certificados corporativos;
- `config.properties` real o configuraciones privadas de profiles;
- workspaces, datos de usuario, payloads o descargas;
- copias de bases de datos internas de Codex;
- manuales completos de creación de Components o Pocketools.

Los manuales de catálogo se mantienen junto al contrato que describen:

- `eap-components/CREAR_COMPONENTES.md`
- `eap-pocketools/CREAR_POCKETOOLS.md`

## Cuándo actualizar `spec/`

Actualice estos documentos cuando cambie alguno de estos elementos:

- el modelo de profile, datos o workspace;
- el límite entre el core y los repositorios externos;
- un formato persistente o contrato público;
- una garantía de seguridad, exportación o recuperación;
- el proceso de bootstrap, actualización o release;
- el objetivo de la siguiente versión estable.

Los detalles puramente mecánicos deben vivir en código, pruebas o en el manual
del contrato correspondiente.
