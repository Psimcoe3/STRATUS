import re
import os

from stratus_logger import get_logger
logger = get_logger(__name__)

# Define your target section names
targets = [
    "eVolve.Core.Revit.HangerPlacement",
    "eVolve.Core.Revit.Integration",
    "eVolve.Core.Revit.Points",
    "eVolve.Core.Revit.Points.Interfaces",
    "eVolve.Core.Revit.ProductInfo",
    "eVolve.Core.Revit.Reporting",
    "eVolve.Core.Revit.Spooling",
    "eVolve.Core.Revit.Utilities",
    "eVolve.Licensing",
    "ExcelWrapper",
]

input_file = "combined_310files.md"
logger.debug("Number of target namespaces: %d", len(targets))
logger.debug("Input file to read: %s", input_file)

# Read the combined markdown file
logger.debug("Reading input file: %s", input_file)
with open(input_file, "r", encoding="utf-8", errors="ignore") as f:
    content = f.read()

# For each target, extract all sections containing that namespace/class
for target in targets:
    logger.debug("Processing target namespace: %s", target)
    # Find all relevant sections: Look for lines containing the namespace/class name
    # and grab some surrounding context for more complete docs.
    pattern = re.compile(
        rf"(<!-- Start of [\s\S]+?)(?:{re.escape(target)})([\s\S]+?)(?=<!-- Start of |\Z)",
        re.IGNORECASE
    )
    matches = pattern.findall(content)
    logger.debug("Matches found for %s: %d", target, len(matches))

    # Combine all found blocks
    if matches:
        output = "\n\n".join("".join(m) for m in matches)
        out_path = f"{target.replace('.', '_')}.md"
        with open(out_path, "w", encoding="utf-8") as out_f:
            out_f.write(output)
        file_size = os.path.getsize(out_path)
        logger.debug("Wrote file: %s (%d bytes)", out_path, file_size)
        logger.info("✅ Wrote: %s", out_path)
    else:
        logger.warning("⚠️  No matches found for %s", target)

logger.info("Done.")
