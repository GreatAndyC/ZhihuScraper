from input_normalizer import normalize_question_input, normalize_user_input


def test_normalize_question_id_passthrough():
    assert normalize_question_input("2009611085918013365") == "2009611085918013365"


def test_normalize_question_url():
    assert (
        normalize_question_input("https://www.zhihu.com/question/2009611085918013365")
        == "2009611085918013365"
    )


def test_normalize_question_answer_url():
    assert (
        normalize_question_input("https://www.zhihu.com/question/2009611085918013365/answer/123456789")
        == "2009611085918013365"
    )


def test_normalize_user_url():
    assert normalize_user_input("https://www.zhihu.com/people/ming--li") == "ming--li"


def test_normalize_user_raw_token():
    assert normalize_user_input("ming--li") == "ming--li"
