import os
import subprocess
import shutil

SVGS = [
    "dark_mode.svg",
    "light_mode.svg",
]

def convert():
    if shutil.which("rsvg-convert"):
        for svg in SVGS:
            png = svg.replace(".svg", ".png")
            cmd = ["rsvg-convert", "-w", "1200", "-h", "850", svg, "-o", png]
            subprocess.run(cmd, check=True)
            print(f"Rendered {png} with rsvg-convert")
    else:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for filename in SVGS:
                    path = os.path.abspath(filename)
                    page = browser.new_page(viewport={"width": 1200, "height": 850})
                    page.goto(f"file://{path}", wait_until="networkidle")
                    png_bytes = page.screenshot()
                    png_name = filename.replace(".svg", ".png")
                    with open(png_name, "wb") as f:
                        f.write(png_bytes)
                    print(f"Rendered {png_name} with Playwright")
                    page.close()
                browser.close()
        except ImportError:
            print("Neither rsvg-convert nor Playwright is available!")

if __name__ == "__main__":
    convert()
