FROM public.ecr.aws/lambda/python:3.13

# poppler-utils (pdftoppm) is needed by pdf2image to render the bond prices
# PDF to images for Textract -- available in AL2023's own repo directly, no
# extra binary bundling needed (see src/bond_data.py for why the pipeline
# uses Textract's table extraction rather than pytesseract/tesseract).
RUN microdnf install -y poppler-utils && microdnf clean all

# Trimmed dependency set (see requirements-lambda.txt for what's excluded and why)
COPY aws/requirements-lambda.txt ${LAMBDA_TASK_ROOT}/requirements-lambda.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-lambda.txt

# App code (read-only at runtime; only /tmp is writable in Lambda)
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY templates/ ${LAMBDA_TASK_ROOT}/templates/
COPY manual_input/ ${LAMBDA_TASK_ROOT}/manual_input/
COPY aws/lambda_handler.py ${LAMBDA_TASK_ROOT}/lambda_handler.py

CMD ["lambda_handler.handler"]
