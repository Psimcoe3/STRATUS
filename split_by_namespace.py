import re

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

# Read the combined markdown file
with open("combined_310files.md", "r", encoding="utf-8", errors="ignore") as f:
    content = f.read()

# For each target, extract all sections containing that namespace/class
for target in targets:
    # Find all relevant sections: Look for lines containing the namespace/class name
    # and grab some surrounding context for more complete docs.
    pattern = re.compile(
        rf"(<!-- Start of [\s\S]+?)(?:{re.escape(target)})([\s\S]+?)(?=<!-- Start of |\Z)",
        re.IGNORECASE
    )
    matches = pattern.findall(content)

    # Combine all found blocks
    if matches:
        output = "\n\n".join("".join(m) for m in matches)
        with open(f"{target.replace('.', '_')}.md", "w", encoding="utf-8") as out_f:
            out_f.write(output)
        print(f"✅ Wrote: {target.replace('.', '_')}.md")
    else:
        print(f"⚠️  No matches found for {target}")

print("Done.")
