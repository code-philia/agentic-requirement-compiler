---
name: non-leaf-ui-only-design
description: Improve a parent requirement's real layout and navigation without modeling UI interfaces.
---

# non-leaf-ui-only-design

1. Work directly on the existing shell/page, route layout, and styles described by the visual reference. Preserve existing child UI and request wiring.
   For web layouts, implement styling through Tailwind CSS v4 utility classes in component className. Do not move shell/page/component rules into index.css or new CSS files. Preserve @import "tailwindcss" in index.css and its main.tsx import; only necessary global theme/fonts/base rules belong there. Use full literal class names rather than dynamic fragments.
2. Complete the parent layout and navigation in application code. Do not create UI interface records, UI nodes, attachment plans, or backend contracts.
3. Leave executable child feature behavior to its leaf DESIGN/TDD. Do not replace child UI with placeholders or pre-implement child business behavior.
4. Do not copy screenshot business data or fabricate runtime state.
5. Return summary and files with frontend/shared paths and empty API/FUNC/DB lists. These paths are the handoff to child nodes.
6. Inspect only relevant frontend entrypoints, components, and styles. Do not explore backend or database files for layout work.
