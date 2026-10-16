
def short_symbol(symbol):
    # Quitar la cotización final para analizar el nombre.
    base = symbol[:-5] if symbol.endswith("-USDT") else symbol

    # Acciones tokenizadas: NCSKSKHYNIX2USD -> SKHYNIX
    if base.startswith("NCSK") and base.endswith("2USD"):
        return base[4:-4]

    # Pares especiales de divisas: NCFXEUR2GBP -> EURGBP
    if base.startswith("NCFX") and base.endswith("2GBP"):
        return base[4:-4] + "GBP"

    # Otros pares normales.
    return base
    
