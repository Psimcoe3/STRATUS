import os
import subprocess
from tkinter import Tk, filedialog, simpledialog
try:
    import pandas as pd
except ImportError:
    pd = None
import xml.etree.ElementTree as ET
import json

from stratus_logger import get_logger
logger = get_logger(__name__)

ALLOWED_EXTS = {'.md', '.csv', '.xlsx', '.xls', '.htm', '.html', '.json', '.xml', '.txt'}

def prompt_select_mode():
    logger.debug("prompt_select_mode() called")
    root = Tk()
    root.withdraw()
    mode = simpledialog.askstring("Select Mode", "Type 'folder' to select a folder, or 'files' to pick specific files:")
    root.destroy()
    result = mode.lower().strip() if mode else None
    logger.debug("User selected mode: %s", result)
    return result

def select_input_files():
    logger.debug("select_input_files() called")
    mode = prompt_select_mode()
    logger.debug("Mode selected: %s", mode)
    paths = []
    root = Tk()
    root.withdraw()
    if mode == 'folder':
        folder = filedialog.askdirectory(title="Select folder to process")
        if folder:
            for fname in os.listdir(folder):
                fpath = os.path.join(folder, fname)
                if os.path.isfile(fpath) and os.path.splitext(fname)[1].lower() in ALLOWED_EXTS:
                    paths.append(fpath)
    elif mode == 'files':
        filetypes = [("Supported Documents", "*.md *.csv *.xlsx *.xls *.htm *.html *.json *.xml *.txt"),
                     ("All files", "*.*")]
        selected = filedialog.askopenfilenames(title="Select files to merge", filetypes=filetypes)
        paths = list(selected)
    root.destroy()
    logger.debug("Number of files found: %d", len(paths))
    return paths

def parse_xml_to_md(path):
    logger.debug("parse_xml_to_md() called with path=%s", path)
    try:
        tree = ET.parse(path)
        texts = [text for text in tree.itertext()]
        logger.debug("Successfully parsed XML: %s", os.path.basename(path))
        return "\n\n".join(filter(None, [t.strip() for t in texts]))
    except Exception as e:
        logger.error("Error parsing XML %s: %s", os.path.basename(path), e, exc_info=True)
        return f"\n❗ Error parsing XML {os.path.basename(path)}: {e}\n"

def convert_file(path):
    ext = os.path.splitext(path)[1].lower()
    logger.debug("convert_file() called with path=%s, detected ext=%s", path, ext)
    # Markdown and plain text: just read
    if ext == '.md' or ext == '.txt':
        logger.debug("Conversion path: markdown/plain text read for %s", os.path.basename(path))
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
        except Exception as e:
            logger.error("Error reading %s: %s", os.path.basename(path), e, exc_info=True)
            return f"\n❗ Error reading {os.path.basename(path)}: {e}\n"
    # HTML/HTM: use correct Pandoc input format
    elif ext in ['.html', '.htm']:
        logger.debug("Conversion path: HTML via Pandoc for %s", os.path.basename(path))
        cmd = ['pandoc', '-f', 'html', '-t', 'markdown', path]
        result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
        if result.returncode == 0:
            return result.stdout
        logger.error("Pandoc error on %s: %s", os.path.basename(path), result.stderr)
        return f"\n❗ Pandoc error on {os.path.basename(path)}:\n{result.stderr}\n"
    # CSV: use pandas
    elif ext == '.csv':
        logger.debug("Conversion path: CSV via pandas for %s", os.path.basename(path))
        if not pd:
            logger.error("Pandas not installed: skipped %s", os.path.basename(path))
            return f"\n❗ Pandas not installed: skipped {os.path.basename(path)}\n"
        try:
            df = pd.read_csv(path)
            return df.to_markdown(index=False)
        except Exception as e:
            logger.error("Error reading CSV %s: %s", os.path.basename(path), e, exc_info=True)
            return f"\n❗ Error reading CSV {os.path.basename(path)}: {e}\n"
    # Excel: use pandas
    elif ext in ['.xlsx', '.xls']:
        logger.debug("Conversion path: Excel via pandas for %s", os.path.basename(path))
        if not pd:
            logger.error("Pandas not installed: skipped %s", os.path.basename(path))
            return f"\n❗ Pandas not installed: skipped {os.path.basename(path)}\n"
        try:
            df = pd.read_excel(path)
            return df.to_markdown(index=False)
        except Exception as e:
            logger.error("Error reading spreadsheet %s: %s", os.path.basename(path), e, exc_info=True)
            return f"\n❗ Error reading spreadsheet {os.path.basename(path)}: {e}\n"
    # JSON: pretty-print as markdown code block
    elif ext == '.json':
        logger.debug("Conversion path: JSON for %s", os.path.basename(path))
        try:
            with open(path, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            return "```json\n" + json.dumps(obj, indent=2) + "\n```"
        except Exception as e:
            logger.error("Error reading JSON %s: %s", os.path.basename(path), e, exc_info=True)
            return f"\n❗ Error reading JSON {os.path.basename(path)}: {e}\n"
    # XML: try docbook (Pandoc), fallback to text
    elif ext == '.xml':
        logger.debug("Conversion path: XML via Pandoc (docbook) for %s", os.path.basename(path))
        cmd = ['pandoc', '-f', 'docbook', '-t', 'markdown', path]
        result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        logger.debug("Pandoc docbook failed or empty, falling back to parse_xml_to_md for %s", os.path.basename(path))
        return parse_xml_to_md(path)
    else:
        logger.warning("Unsupported file type: %s", ext)
        return f"\n❗ Unsupported file type: {ext}\n"

def combine_selected_files():
    logger.debug("combine_selected_files() called")
    paths = select_input_files()
    if not paths:
        logger.info("❌ No files selected. Exiting.")
        return
    output = os.path.join(os.getcwd(), f"combined_{len(paths)}files.md")
    logger.debug("Output path: %s", output)
    with open(output, 'w', encoding='utf-8') as out:
        for path in paths:
            base = os.path.basename(path)
            logger.info("Processing: %s", base)
            logger.debug("Processing file: %s", path)
            out.write(f"\n\n<!-- Start of {base} -->\n\n")
            out.write(convert_file(path))
    logger.info("✅ Combined %d files into:\n  %s", len(paths), output)

if __name__ == '__main__':
    combine_selected_files()
