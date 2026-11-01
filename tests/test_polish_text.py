import unittest

from polish_text import count


class PolishTextTests(unittest.TestCase):
    def test_counts_and_unknown_value(self):
        forms = ('obiekt', 'obiekty', 'obiektów')
        expected = {0:'obiektów', 1:'obiekt', 2:'obiekty', 4:'obiekty', 5:'obiektów',
                    11:'obiektów', 12:'obiektów', 14:'obiektów', 21:'obiektów', 22:'obiekty',
                    101:'obiektów', 112:'obiektów', 122:'obiekty'}
        for number, noun in expected.items():
            with self.subTest(number=number):
                self.assertEqual(count(number, forms), f'{number} {noun}')
        self.assertEqual(count(None, forms), '— obiektów')
