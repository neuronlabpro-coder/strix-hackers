"""Webhooks salientes de Mind Guard Fenix Team: registro, firma y despacho.

La app se separa de `repositories` aunque los dos manejen webhooks, porque en ambos
casos *secreto* significa cosas distintas. Los entrantes de GitHub verifican **la** firma llega y no
guardan ningun secreto: el endpoint es publico y quien lo llama ya tiene credenciales. Los
salientes guardan un secreto propio para **firmar** lo que envian, lo que significa que
ese secreto hay que proteger en reposo, limitar quien lo lee y rotar cuando el receptor
cambia. Juntarlos en un módulo haría que un cambio en la lógica de firma acabara tocando
la verificación de un webhook entrante que funciona, que es la forma mas cara de romper
algo que ya estaba bien.
"""
