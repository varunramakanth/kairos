"""Write the OpenAPI schema to a file: ``python -m kairos_api.openapi OUT``.

The schema is produced from the app object without starting it, so no database is
needed. ``just gen-client`` runs this and then generates the TypeScript client.
"""

import json
import sys
from pathlib import Path

from kairos_api.app import create_app
from kairos_api.settings import ApiSettings


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("apps/web/openapi.json")
    schema = create_app(ApiSettings()).openapi()
    out.write_text(json.dumps(schema, indent=2) + "\n")
    sys.stdout.write(f"wrote {out}\n")


if __name__ == "__main__":
    main()
