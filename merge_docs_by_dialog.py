import os
import subprocess
from tkinter import Tk, filedialog, simpledialog
try:
    import pandas as pd
except ImportError:
    pd = None
import xml.etree.ElementTree as ET
import json

ALLOWED_EXTS = {'.md', '.csv', '.xlsx', '.xls', '.htm', '.html', '.json', '.xml', '.txt'}

def prompt_select_mode():
    root = Tk()
    root.withdraw()
    mode = simpledialog.askstring("Select Mode", "Type 'folder' to select a folder, or 'files' to pick specific files:")
    root.destroy()
    return mode.lower().strip() if mode else None

def select_input_files():
    mode = prompt_select_mode()
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
    return paths

def parse_xml_to_md(path):
    try:
        tree = ET.parse(path)
        texts = [text for text in tree.itertext()]
        return "\n\n".join(filter(None, [t.strip() for t in texts]))
    except Exception as e:
        return f"\n❗ Error parsing XML {os.path.basename(path)}: {e}\n"

def convert_file(path):
    ext = os.path.splitext(path)[1].lower()
    # Markdown and plain text: just read
    if ext == '.md' or ext == '.txt':
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
        except Exception as e:
            return f"\n❗ Error reading {os.path.basename(path)}: {e}\n"
    # HTML/HTM: use correct Pandoc input format
    elif ext in ['.html', '.htm']:
        cmd = ['pandoc', '-f', 'html', '-t', 'markdown', path]
        result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
        return result.stdout if result.returncode == 0 else f"\n❗ Pandoc error on {os.path.basename(path)}:\n{result.stderr}\n"
    # CSV: use pandas
    elif ext == '.csv':
        if not pd:
            return f"\n❗ Pandas not installed: skipped {os.path.basename(path)}\n"
        try:
            df = pd.read_csv(path)
            return df.to_markdown(index=False)
        except Exception as e:
            return f"\n❗ Error reading CSV {os.path.basename(path)}: {e}\n"
    # Excel: use pandas
    elif ext in ['.xlsx', '.xls']:
        if not pd:
            return f"\n❗ Pandas not installed: skipped {os.path.basename(path)}\n"
        try:
            df = pd.read_excel(path)
            return df.to_markdown(index=False)
        except Exception as e:
            return f"\n❗ Error reading spreadsheet {os.path.basename(path)}: {e}\n"
    # JSON: pretty-print as markdown code block
    elif ext == '.json':
        try:
            with open(path, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            return "```json\n" + json.dumps(obj, indent=2) + "\n```"
        except Exception as e:
            return f"\n❗ Error reading JSON {os.path.basename(path)}: {e}\n"
    # XML: try docbook (Pandoc), fallback to text
    elif ext == '.xml':
        cmd = ['pandoc', '-f', 'docbook', '-t', 'markdown', path]
        result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        return parse_xml_to_md(path)
    else:
        return f"\n❗ Unsupported file type: {ext}\n"

def combine_selected_files():
    paths = select_input_files()
    if not paths:
        print("❌ No files selected. Exiting.")
        return
    output = os.path.join(os.getcwd(), f"combined_{len(paths)}files.md")
    with open(output, 'w', encoding='utf-8') as out:
        for path in paths:
            base = os.path.basename(path)
            print(f"Processing: {base}")
            out.write(f"\n\n<!-- Start of {base} -->\n\n")
            out.write(convert_file(path))
    print(f"\n✅ Combined {len(paths)} files into:\n  {output}")

if __name__ == '__main__':
    combine_selected_files()
