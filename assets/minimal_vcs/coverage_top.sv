module top;
  reg clk = 0;
  reg enable = 0;
  reg toggle_bit = 0;
  reg [3:0] data = 0;
  reg [3:0] count = 0;

  initial begin
    $fsdbDumpfile("waves.fsdb");
    $fsdbDumpvars(0, top);
    repeat (12) begin
      #5 clk = ~clk;
      enable = ~enable;
      data = data + 4'd1;
      toggle_bit = ~toggle_bit;
      if (enable && data[0]) begin
        count = count + 4'd2;
      end else begin
        count = count + 4'd1;
      end
    end
    $finish;
  end
endmodule
