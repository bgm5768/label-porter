"""Identical folder/file selectors for original and corrected inputs."""
import tkinter as tk
from tkinter import ttk


class DataInputPanel(ttk.Frame):
    def __init__(self, parent, count, choose_folder, choose_files, remove, preview):
        super().__init__(parent, style='Card.TFrame')
        self.pack(fill='x')
        actions = ttk.Frame(self, style='Card.TFrame'); actions.pack(fill='x', pady=(0, 8))
        self.buttons = []
        for label, command in (('폴더 선택…', choose_folder), ('파일 다중 선택…', choose_files),
                               ('선택 제거', remove), ('JSON 미리보기', preview)):
            button = ttk.Button(actions, text=label, command=command)
            button.pack(side='left', padx=(0, 8)); self.buttons.append(button)
        row = ttk.Frame(self, style='Card.TFrame'); row.pack(fill='x')
        self.list = tk.Listbox(row, height=3, selectmode='extended', exportselection=False,
                              relief='flat', highlightthickness=1, highlightbackground='#dce3ee',
                              background='#f8faff', foreground='#15304d', selectbackground='#dbeafe')
        scroll = ttk.Scrollbar(row, command=self.list.yview); scroll.pack(side='right', fill='y')
        self.list.configure(yscrollcommand=scroll.set); self.list.pack(fill='x', expand=True)
        self.list.bind('<Double-1>', lambda event: preview())
        ttk.Label(self, textvariable=count, style='CardMuted.TLabel').pack(anchor='w', pady=(6, 0))

    def display(self, folder, files):
        self.list.delete(0, 'end')
        if folder:
            self.list.insert('end', f'[폴더 · 하위 폴더 포함] {folder}')
        elif files:
            self.list.insert('end', *files)
