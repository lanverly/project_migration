import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import json
import os
import threading
import subprocess
import time
import logging
import migration_to_plone_6

CONFIG_FILE = "config.json"
PROGRESS_FILE = "migracao_progresso.json"

class TextHandler(logging.Handler):
    """Handler para redirecionar logs do logging para um widget Tkinter Text."""
    def __init__(self, text_widget, callback_after):
        super().__init__()
        self.text_widget = text_widget
        self.callback_after = callback_after

    def emit(self, record):
        msg = self.format(record) + "\n"
        # Usa after para garantir que a atualização da UI ocorra na thread principal
        self.callback_after(0, self.update_text, msg)

    def update_text(self, msg):
        self.text_widget.configure(state='normal')
        
        tags = []
        if "[ERROR]" in msg or "❌" in msg: tags.append("error")
        if "[WARNING]" in msg or "⚠️" in msg: tags.append("warn")
        
        self.text_widget.insert(tk.END, msg, tuple(tags))
        self.text_widget.see(tk.END)
        self.text_widget.configure(state='disabled')

class MigrationApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Migrador Gov.br → Plone 6")
        self.root.geometry("800x700")
        self.root.minsize(600, 500)
        
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        self.process_running = False
        self.load_config()
        self.create_widgets()
        self.setup_internal_logging()

    def setup_internal_logging(self):
        # Configura o logger do script para usar o nosso custom handler
        self.handler = TextHandler(self.log_area, self.root.after)
        self.handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        
        # Pega o logger do módulo de migração e adiciona o handler
        t_log = logging.getLogger("trensurb_migrar_noticias")
        t_log.addHandler(self.handler)
        t_log.setLevel(logging.INFO)
        
        # Também configurar o logger raiz para garantir captura se necessário
        root_log = logging.getLogger()
        root_log.addHandler(self.handler)
        root_log.setLevel(logging.INFO)

    def load_config(self):
        self.config_data = {
            "plone_url": "",
            "plone_token": "",
            "plone_news_folder": "/assuntos/noticias",
            "source_base": "https://www.gov.br",
            "source_start": "https://www.gov.br/trensurb/pt-br/assuntos/noticias",
            "delay": 2,
            "max_news": 0,
            "all_pages": True,
            "progress_file": "migracao_progresso.json",
            "portal_type": "Document",
            "migrate_as_self": False,
            "skip_files": False
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    self.config_data.update(json.load(f))
            except Exception as e:
                print(f"Erro ao carregar config: {e}")

    def save_config(self):
        self.config_data["plone_url"] = self.entry_url.get()
        self.config_data["plone_token"] = self.entry_token.get()
        self.config_data["plone_news_folder"] = self.entry_folder.get()
        self.config_data["source_start"] = self.entry_source.get()
        
        try:
            self.config_data["max_news"] = int(self.entry_max.get())
        except ValueError:
            self.config_data["max_news"] = 0

        self.config_data["all_pages"] = self.var_all_pages.get()
        self.config_data["migrate_as_self"] = self.var_migrate_as_self.get()
        self.config_data["skip_files"] = self.var_skip_files.get()
        self.config_data["portal_type"] = "News Item" if self.cmb_type.get() == "Notícia (News Item)" else "Document"
        
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config_data, f, indent=4, ensure_ascii=False)

    def create_widgets(self):
        # Frame Principal
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Sessão de Configurações
        config_lf = ttk.LabelFrame(main_frame, text=" Configurações do Plone ", padding="10")
        config_lf.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(config_lf, text="URL do Plone 6 Destino:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.entry_url = ttk.Entry(config_lf, width=50)
        self.entry_url.grid(row=0, column=1, sticky=tk.EW, pady=2, padx=5)
        self.entry_url.insert(0, self.config_data.get("plone_url", ""))

        ttk.Label(config_lf, text="Token Bearer (JWT):").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.entry_token = ttk.Entry(config_lf, width=50)
        self.entry_token.grid(row=1, column=1, sticky=tk.EW, pady=2, padx=5)
        self.entry_token.insert(0, self.config_data.get("plone_token", ""))

        ttk.Label(config_lf, text="Pasta Destino (ex: /noticias):").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.entry_folder = ttk.Entry(config_lf, width=50)
        self.entry_folder.grid(row=2, column=1, sticky=tk.EW, pady=2, padx=5)
        self.entry_folder.insert(0, self.config_data.get("plone_news_folder", ""))
        
        # Novas Opções de Migração Avançada
        opts_frame = ttk.Frame(config_lf)
        opts_frame.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=5)
        
        self.var_migrate_as_self = tk.BooleanVar(value=self.config_data.get("migrate_as_self", False))
        self.chk_self = ttk.Checkbutton(opts_frame, text="Modo Pasta (Lista de Documentos/PDFs)", variable=self.var_migrate_as_self)
        self.chk_self.pack(side=tk.LEFT, padx=(0, 20))
        
        self.var_skip_files = tk.BooleanVar(value=self.config_data.get("skip_files", False))
        self.chk_skip = ttk.Checkbutton(opts_frame, text="Pular Uploads (Re-vincular apenas)", variable=self.var_skip_files)
        self.chk_skip.pack(side=tk.LEFT)

        # Sessão de Origem
        source_lf = ttk.LabelFrame(main_frame, text=" Configurações de Origem (Gov.br) ", padding="10")
        source_lf.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(source_lf, text="URL da Listagem (Origem):").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.entry_source = ttk.Entry(source_lf, width=50)
        self.entry_source.grid(row=0, column=1, sticky=tk.EW, pady=2, padx=5, columnspan=3)
        self.entry_source.insert(0, self.config_data.get("source_start", ""))

        ttk.Label(source_lf, text="Limite de Notícias (0=Todas):").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.entry_max = ttk.Entry(source_lf, width=10)
        self.entry_max.grid(row=1, column=1, sticky=tk.W, pady=2, padx=5)
        self.entry_max.insert(0, str(self.config_data.get("max_news", 0)))

        self.var_all_pages = tk.BooleanVar(value=self.config_data.get("all_pages", True))
        self.chk_pages = ttk.Checkbutton(source_lf, text="Varrer todas as páginas seguintes", variable=self.var_all_pages)
        self.chk_pages.grid(row=1, column=2, sticky=tk.W, pady=2, padx=5)

        ttk.Label(source_lf, text="Tipo de Conteúdo:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.cmb_type = ttk.Combobox(source_lf, values=["Página (Document)", "Notícia (News Item)"], state="readonly", width=30)
        self.cmb_type.grid(row=2, column=1, sticky=tk.W, pady=2, padx=5, columnspan=2)
        current_type = self.config_data.get("portal_type", "Document")
        self.cmb_type.set("Notícia (News Item)" if current_type == "News Item" else "Página (Document)")

        # Botões de Ação
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        self.btn_run = ttk.Button(btn_frame, text="▶ Iniciar Migração", command=self.start_migration)
        self.btn_run.pack(side=tk.LEFT, padx=5)
        
        self.btn_stop = ttk.Button(btn_frame, text="⏹ Parar", command=self.stop_migration, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=5)

        self.btn_clear = ttk.Button(btn_frame, text="🗑 Limpar Progresso", command=self.clear_progress)
        self.btn_clear.pack(side=tk.RIGHT, padx=5)

        # Rodapé (fixed at bottom)
        footer_frame = ttk.Frame(main_frame)
        footer_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 0))
        ttk.Label(footer_frame, text="Desenvolvido por Byron Lanverly", font=("Arial", 9, "italic"), foreground="gray").pack(side=tk.RIGHT)

        # Logs (takes remaining space)
        log_lf = ttk.LabelFrame(main_frame, text=" Console de Execução ", padding="5")
        log_lf.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.log_area = scrolledtext.ScrolledText(log_lf, wrap=tk.WORD, bg="black", fg="lightgreen", font=("Consolas", 10))
        self.log_area.pack(fill=tk.BOTH, expand=True)
        
        config_lf.columnconfigure(1, weight=1)
        source_lf.columnconfigure(1, weight=1)

    def write_log(self, msg):
        self.log_area.tag_configure('error', foreground='red')
        self.log_area.tag_configure('warn', foreground='yellow')
        
        # Colorize basic keywords mapping to terminal colors
        tags = []
        if "[ERROR]" in msg or "❌" in msg: tags.append("error")
        if "[WARNING]" in msg or "⚠️" in msg: tags.append("warn")
            
        self.log_area.insert(tk.END, msg, tuple(tags))
        self.log_area.see(tk.END)

    def clear_progress(self):
        if messagebox.askyesno("Limpar Progresso", "Tem certeza que deseja apagar o registro de notícias concluídas?\nA migração começará da primeira página novamente."):
            if os.path.exists(PROGRESS_FILE):
                os.remove(PROGRESS_FILE)
            self.write_log(f"\n--- Progresso ({PROGRESS_FILE}) apagado com sucesso ---\n")

    def start_migration(self):
        self.save_config()
        self.log_area.delete(1.0, tk.END)
        self.write_log("Salvando configurações...\nIniciando Script de Migração...\n\n")
        
        self.btn_run.config(state=tk.DISABLED)
        self.btn_clear.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        
        self.process_running = True
        
        # Iniciar thread para não congelar a janela
        self.thread = threading.Thread(target=self.run_internal_task, daemon=True)
        self.thread.start()

    def run_internal_task(self):
        try:
            # Sincroniza a configuração do app com o módulo
            migration_to_plone_6.CONFIG = self.config_data
            migration_to_plone_6.STOP_SIGNAL = False # Garante que comece resetado
            
            # Executa a função principal do módulo
            migration_to_plone_6.main()
            
        except Exception as e:
            self.root.after(0, self.write_log, f"\n[ERRO CRÍTICO] {e}\n")
        finally:
            self.process_running = False
            self.root.after(0, self.process_finished)

    def run_process(self):
        # Este método não é mais usado, mantido apenas se necessário para retrocompatibilidade rápida
        pass

    def stop_migration(self):
        # Sinaliza interrupção suave para o loop interno
        migration_to_plone_6.STOP_SIGNAL = True
        self.write_log("\n🛑 PARADA SOLICITADA. Aguardando fim do item atual para encerrar com segurança...\n")
        self.btn_stop.config(state=tk.DISABLED)

    def process_finished(self):
        self.btn_run.config(state=tk.NORMAL)
        self.btn_clear.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.write_log("\n--- Execução Finalizada ---\n")

if __name__ == "__main__":
    root = tk.Tk()
    app = MigrationApp(root)
    root.mainloop()
