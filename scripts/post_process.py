import os
import re

def clean_java_files(root_dir):
    print("[*] Iniciando saneamiento de archivos Java...")
    count = 0
    for subdir, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith(".java"):
                file_path = os.path.join(subdir, file)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()

                    cleaned_content = re.sub(r'/\* Dump dbs parameters.*?\*/', '', content)

                    if content != cleaned_content:
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(cleaned_content)
                        count += 1
                except Exception:
                    pass
    print(f"[*] Saneamiento completado. Archivos optimizados: {count}")

if __name__ == "__main__":
    if os.path.exists("output_project"):
        clean_java_files("output_project")
